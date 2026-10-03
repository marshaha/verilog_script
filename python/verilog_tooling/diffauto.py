"""verilog-mode ``verilog-diff-auto`` port: show AUTO expansion differences.

Expands AUTOs in a temporary copy of the buffer (the full AALL pipeline,
i.e. what ``verilog-batch-auto`` would produce) and reports any change,
like ``verilog-diff-buffers-p`` with whitespace ignored for *detection*:
once a difference is detected, the real differing lines are shown.

Exit status is 0 either way; the unified diff goes to stdout (empty when
the buffer is already fully expanded).
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path


def _squash(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def diff_auto(
    lines: list[str],
    *,
    ref_file: str | None = None,
    libdirs: list[str] | None = None,
    interfaces: list[str] | None = None,
) -> str:
    """Unified diff between LINES and their fully AUTO-expanded form.

    Detection ignores whitespace-only changes; the shown hunks keep the
    original text on both sides.
    """
    from .inst import _main_aall

    args = argparse.Namespace(
        command="aall",
        in_file=ref_file or "<buffer>",
        ref_file=ref_file,
        libdir=libdirs or [],
        interface=interfaces or [],
        which=None,
        date=None,
        sort=False,
        dot_name=False,
        param_value=False,
        star_expand=False,
        star_save=False,
    )
    expanded = _main_aall("\n".join(lines), list(lines), args)
    old_sq = [_squash(l) for l in lines]
    new_sq = [_squash(l) for l in expanded]
    if old_sq == new_sq:
        return ""
    return "".join(
        difflib.unified_diff(
            [l + "\n" for l in lines],
            [l + "\n" for l in expanded],
            fromfile="current",
            tofile="auto-expanded",
        )
    )


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.diffauto",
        description="verilog-mode verilog-diff-auto rewrite",
    )
    parser.add_argument("command", choices=["diff"], help="diff: show AUTO expansion differences")
    parser.add_argument("-i", "--in_file", required=True, help="buffer file")
    parser.add_argument(
        "-o", "--out_file", default=None, help="write unified diff here (default: stdout)"
    )
    parser.add_argument("--ref_file", default=None)
    parser.add_argument("-y", "--libdir", action="append", default=[])
    parser.add_argument("-I", "--interface", action="append", default=[])
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    text = diff_auto(
        lines, ref_file=args.ref_file, libdirs=args.libdir, interfaces=args.interface
    )
    if args.out_file:
        Path(args.out_file).write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
