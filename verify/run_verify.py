#!/usr/bin/env python3
"""Closed-loop verification: my extended-AALL vs stock emacs verilog-mode.

For each test file in the manifest:
  1. emacs --batch runs gen_golden.el  (delete-auto -> auto, NO indent)  -> golden/
  2. python -m verilog_tooling.inst aall (the real user-facing command)   -> mine/
  3. both sides normalized (untabify + rstrip trailing whitespace), then diff.

A PASS means byte-identical AUTO expansion vs emacs.  Indentation is
deliberately out of scope (vim has its own indent engine).

Usage:
  python3 verify/run_verify.py [--emacs-only | --mine-only] [filter...]
  (run from ~/workspace/verilog_script; VMODE=/tmp/vcmp/verilog-mode by default)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VMODE = Path(os.environ.get("VMODE", "/tmp/vcmp/verilog-mode"))
GOLDEN = HERE / "golden"
MINE = HERE / "mine"

# markers introduced by the newly implemented features -> owning test files
NEW_MARKERS = [
    "AUTOSENSE", "/*AS*/",
    "AUTORESET",
    "AUTOTIEOFF",
    "AUTOUNUSED",
    "AUTOUNDEF",
    "AUTOASCIIENUM",
    "AUTOLOGIC",
    "AUTOINOUTMODULE", "AUTOINOUTCOMP", "AUTOINOUTIN",
    "AUTOINOUTMODPORT", "AUTOINOUTPARAM",
    "AUTOASSIGNMODPORT",
    "AUTOOUTPUTEVERY",
    "AUTOREGINPUT",
    "AUTOINSERTLISP", "AUTOINSERTLAST",
]

# test files whose goldens depend on real elisp eval (user-defined lisp fns);
# verified separately with equivalent shell-command cases
SKIP_EMACS = {"ExampInsertLisp.v", "autoinsertlast_1.v", "autolisp_truex.v",
              "autolisp_order_bug356.v",
              # AUTO_TEMPLATE calls a user defun (vl-prefix-i-o); the port's
              # documented Python/elisp subset does not cover defun/cond
              "autoinst_ma_io_prefix.v"}


def build_manifest() -> list[str]:
    tests = VMODE / "tests"
    ok = VMODE / "tests_ok"
    manifest = []
    for f in sorted(ok.iterdir()):
        if not f.is_file():
            continue
        src = tests / f.name
        if not src.exists():
            continue
        text = src.read_text(errors="replace")
        if f.name in SKIP_EMACS:
            continue
        if any(m in text for m in NEW_MARKERS):
            manifest.append(f.name)
    return manifest


def untabify_rstrip(text: str) -> str:
    out = []
    for line in text.splitlines():
        line = line.expandtabs(8).rstrip()
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def gen_emacs_golden(name: str) -> Path:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    out = GOLDEN / name
    if out.exists():
        return out
    cmd = [
        "emacs", "--batch", "--no-site-file",
        "-l", str(VMODE / "verilog-mode.el"),
        "-l", str(HERE / "gen_golden.el"),
        "--eval", f'(gen-golden "tests/{name}" "{out}")',
    ]
    r = subprocess.run(cmd, cwd=VMODE, capture_output=True, text=True, timeout=300)
    if not out.exists():
        print(f"  [emacs] FAILED to produce {name}\n{r.stderr[-2000:]}")
        raise SystemExit(1)
    return out


def gen_mine(name: str) -> Path:
    MINE.mkdir(parents=True, exist_ok=True)
    out = MINE / name
    src = VMODE / "tests" / name
    cmd = [sys.executable, "-m", "verilog_tooling.inst", "aall",
           "-i", str(src), "-o", str(out),
           "-y", str(VMODE / "tests")]
    env = {**os.environ, "PYTHONPATH": str(REPO / "python"),
           "VERILOG_TOOLING_QUIET": "1"}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    if r.returncode != 0 or not out.exists():
        print(f"  [mine] FAILED on {name}\n{r.stderr[-2000:]}")
        raise SystemExit(1)
    # normalize like the emacs side (untabify + trailing ws already stripped by tool)
    out.write_text(untabify_rstrip(out.read_text()))
    return out


def main(argv: list[str]) -> int:
    only_emacs = "--emacs-only" in argv
    only_mine = "--mine-only" in argv
    filters = [a for a in argv if not a.startswith("--")]
    manifest = [m for m in build_manifest() if not filters or any(f in m for f in filters)]
    print(f"manifest: {len(manifest)} test files")
    passed, failed = [], []
    for name in manifest:
        print(f"--- {name}")
        try:
            golden_p, mine_p = None, None
            if not only_mine:
                golden_p = gen_emacs_golden(name)
                golden_p.write_text(untabify_rstrip(golden_p.read_text()))
            if not only_emacs:
                mine_p = gen_mine(name)
            if golden_p and mine_p:
                r = subprocess.run(["diff", "-u", str(golden_p), str(mine_p)],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    print("  PASS")
                    passed.append(name)
                else:
                    print("  FAIL")
                    (HERE / "diffs").mkdir(exist_ok=True)
                    (HERE / "diffs" / (name + ".diff")).write_text(r.stdout)
                    failed.append(name)
        except SystemExit:
            failed.append(name + " (error)")
    print(f"\n==== {len(passed)} passed, {len(failed)} failed ====")
    for f in failed:
        print("  FAIL:", f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
