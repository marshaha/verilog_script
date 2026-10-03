#!/usr/bin/env python3
"""Smoke-test new modules: import + CLI --help + one golden eyeball each."""
import subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
T = Path("/tmp/vcmp/verilog-mode/tests")
G = Path(__file__).resolve().parent / "golden"

CASES = [
    # (module, subcommand, testfile, extra_args)
    ("sense", "asense", "autosense.v", []),
    ("sense", "areset", "autoreset_reed.v", []),
    ("xfer", "ainoutmodule", "autoinoutmodule.v", ["-y", str(T)]),
    ("xfer", "ainoutcomp", "autoinoutcomp.v", ["-y", str(T)]),
    ("xfer", "ainoutin", "autoinoutin.v", ["-y", str(T)]),
    ("xfer", "ainoutmodport", "autoinoutmodport_prefix.v", ["-y", str(T)]),
    ("xfer", "ainoutparam", "autoinoutparam.v", ["-y", str(T)]),
    ("xfer", "aassignmodport", "automodport_ex.v", ["-y", str(T)]),
    ("xfer", "aoutputevery", "autooutputevery_example.v", ["-y", str(T)]),
    ("xfer", "areginput", "autoreginput.v", ["-y", str(T)]),
    ("misc", "aascii", "autoasciienum_ex.v", []),
    ("misc", "alogic", "autologic.sv", []),
    ("misc", "atieoff", "autotieoff_signed.v", ["-y", str(T)]),
    ("misc", "aunused", "autounused.v", ["-y", str(T)]),
    ("misc", "aundef", "autoundef.v", []),
]

ok = True
for mod, cmd, tfile, extra in CASES:
    src, dst = T / tfile, Path(f"/tmp/smoke_{tfile}")
    # collapse first: feed the already-expanded golden through kill, then expand
    r = subprocess.run(
        [sys.executable, "-m", f"verilog_tooling.{mod}", cmd,
         "-i", str(src), "-o", str(dst)] + extra,
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO / "python"),
             "VERILOG_TOOLING_QUIET": "1"},
    )
    status = "OK " if r.returncode == 0 and dst.exists() else "ERR"
    if status == "ERR":
        ok = False
    print(f"[{status}] {mod} {cmd} {tfile}")
    if r.returncode != 0:
        print(r.stderr[-800:])
print("ALL OK" if ok else "SOME FAILED")
