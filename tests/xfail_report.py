#!/usr/bin/env python3
"""Triage helper for the vm-repo harness backlog.

Reads the results JSON written by tests/test_vm_repo.py (VM_REPO_RESULTS,
default /tmp/vm_repo_results.json) and lists the xfail files sorted by
NON-whitespace diff size against the emacs baseline — the smallest diffs
are almost always single bugs (one parse fix often locks several files).

Usage: python3 tests/xfail_report.py [N]   (N = show top N, default all)
"""

import json
import os
import sys

RESULTS = os.environ.get("VM_REPO_RESULTS", "/tmp/vm_repo_results.json")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    data = json.load(open(RESULTS))
    rows = []
    for name, rec in data.items():
        if rec.get("category") != "content_diff":
            continue
        ws = rec.get("ws_diff_vs_emacs")
        if ws is None:
            ws = rec.get("diff_lines_vs_golden", 999)
        rows.append((ws, name, rec.get("unsupported_localvars") or []))
    rows.sort()
    print(f"{len(rows)} content-diff files, cheapest first:")
    for ws, name, lv in rows[: n or None]:
        tag = f"  [locals: {','.join(lv)}]" if lv else ""
        print(f"  {ws:3d}  {name}{tag}")


if __name__ == "__main__":
    main()
