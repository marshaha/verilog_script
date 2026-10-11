#!/usr/bin/env python3
"""
Scan a project for RTL directories and generate a .vc file.
Usage: python3 scan_project.py <proj_root> <output.vc>
The .vc file lists -y dirs, one per line. Scripts read it for uniform handling.
"""
import sys
from pathlib import Path

def find_rtl_dirs(root):
    root = Path(root)
    dirs = set()
    # Skip patterns
    skip = {"tb", "testbench", "sim", "dv", "tests", "pre_dv", "fpv", "formal"}
    for ext in ["*.sv", "*.v", "*.svh"]:
        for f in root.rglob(ext):
            parts = set(f.parts)
            if parts & skip:
                continue
            # Skip if filename suggests testbench
            if "tb" in f.stem.lower() or "test" in f.stem.lower():
                continue
            dirs.add(str(f.parent))
    return sorted(dirs)

if __name__ == "__main__":
    root = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "project.vc"
    dirs = find_rtl_dirs(root)
    with open(out, "w") as f:
        f.write(f"# Project: {root}\n")
        f.write(f"# Generated: RTL directories for -y flags\n")
        for d in dirs:
            f.write(f"-y {d}\n")
    print(f"Wrote {len(dirs)} dirs to {out}", file=sys.stderr)
