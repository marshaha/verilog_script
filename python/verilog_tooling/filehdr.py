"""Python rewrite of automatic.vim's file-header / new-file-template commands.

Covers the new-file family of automatic.vim:

- ``add_header``    <- AddHeader    (automatic.vim 854-886): prepend the
  ``// +FHDR`` copyright header block to a buffer;
- ``gen_template``  <- AutoTemplate (automatic.vim 888-1002): emit a complete
  new-file skeleton (header + module + Local Variables) by suffix — a
  ``*tb`` name gets the testbench skeleton, anything else the normal one;
- ``get_company`` / ``get_author`` / ``get_vc`` / ``get_pwd`` <- the
  ``$COMPANY`` lookup in AddHeader and s:GetUserName (845-852) /
  s:CheckVc (834-842) / s:GetPwd (809-816) environment helpers.

``add_header`` and ``gen_template`` are pure (lines in -> lines out) with the
company/author/pwd/vc/now values passed in, so tests need no environment.
``add_header`` is also attached to
:class:`~verilog_tooling.inst.VerilogBuffer` on import (like
:mod:`verilog_tooling.fmt`), resolving the unset values from the environment
helpers.

Formatting follows automatic.vim exactly, including the trailing spaces on
``// Last Modified : ``, the ``verilog-library-flags``/``-directories`` lines
and the ``/*autofsm*/`` comment, and the two-space ``-y  `` padding.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from .inst import VerilogBuffer

_FHDR_MARK = "// +FHDR"
_MODULE_WORD = re.compile(r"\w+")


# ---------------------------------------------------------------------------
# environment helpers (s:GetUserName / s:CheckVc / s:GetPwd / $COMPANY)


def get_company(env: Mapping[str, str] = os.environ) -> str:
    """``$COMPANY``; '' when unset (the Vim original only echoes a warning)."""
    return env.get("COMPANY", "")


def get_author(env: Mapping[str, str] = os.environ) -> str:
    """``$USER_DIT`` unless unset or a literal ``USER_DIT:`` fallback, else ``$USER``."""
    user = env.get("USER_DIT", "")
    if user and not user.startswith("USER_DIT:"):
        return user
    return env.get("USER", "")


def get_vc(env: Mapping[str, str] = os.environ) -> str:
    """``$EMACS_VC`` unless unset or a literal ``EMACS_VC:`` fallback, else ''."""
    vc = env.get("EMACS_VC", "")
    if vc and not vc.startswith("EMACS_VC:"):
        return vc
    return ""


def get_pwd(env: Mapping[str, str] = os.environ) -> str:
    """``$PWD``, falling back to the process working directory."""
    return env.get("PWD") or os.getcwd()


# ---------------------------------------------------------------------------
# AddHeader


def _header_lines(
    filename: str, company: str, author: str, now: datetime.datetime
) -> list[str]:
    return [
        "// +FHDR----------------------------------------------------------------------",
        f"//                 Copyright (c) {now.year} {company}.",
        "//                     ALL RIGHTS RESERVED",
        f"//  This source file is the property of {company}  Technology Co., Ltd. and",
        "//  may not be copied or distributed in any isomorphic form without the prior",
        f"//  written consent of {company} Technology Co., Ltd.",
        "// ---------------------------------------------------------------------------",
        f"// Filename      : {filename}",
        f"// Author        : {author}",
        f"// Created On    : {now:%Y-%m-%d %H:%M}",
        "// Last Modified : ",
        "// ---------------------------------------------------------------------------",
        "// Description:",
        "//",
        "//",
        "// -FHDR----------------------------------------------------------------------",
        "",
    ]


def add_header(
    lines: Sequence[str],
    filename: str,
    company: str,
    author: str,
    now: datetime.datetime,
) -> list[str]:
    """Prepend the ``// +FHDR`` header block to LINES.

    Idempotent: when the first line already holds the ``// +FHDR`` mark the
    buffer is returned unchanged.
    """
    lines = list(lines)
    if lines and _FHDR_MARK in lines[0]:
        return lines
    return _header_lines(filename, company, author, now) + lines


# ---------------------------------------------------------------------------
# AutoTemplate


def _module_name(filename: str) -> str:
    """First ``\\w+`` word of the basename (automatic.vim's matchstr(filename))."""
    m = _MODULE_WORD.search(os.path.basename(filename))
    return m.group(0) if m else ""


def _tb_skeleton(modulename: str) -> list[str]:
    return [
        "/*autoreginput*/",
        "",
        "",
        "reg                                     clk;",
        "reg                                     rst_n;",
        "/*autodef off*/",
        "initial begin",
        "    clk = 1'b0;",
        "    forever #10 clk = ~clk;",
        "end",
        "initial begin",
        "    rst_n = 1'b0;",
        "    #52 rst_n = 1'b1;",
        "end",
        "initial begin",
        '    $fsdbDumpfile("main.fsdb") ;',
        f'    $fsdbDumpvars(0,{modulename},"+mda");',
        "end",
        "initial begin",
        "    #1000;",
        "    $finish;",
        "end",
        "/*autodef on*/",
        "",
        "//{{{",
        "/*autodef*/",
        "/*autowire*/",
        "/*autoreg*/",
        "//}}}",
        "",
        "//inst u_inst(/*autoinst*/);",
        "",
    ]


def _module_skeleton() -> list[str]:
    return [
        "/*autoDISABLEinput*/",
        "/*autoDISABLEoutput*/",
        "/*autoDISABLEinout*/",
        "",
        "input                                   clk;",
        "input                                   rst_n;",
        "",
        "input                                   vld;",
        "input        [7:0]                      data;",
        "output                                  ack;",
        "",
        "//{{{",
        "/*autodef*/",
        "/*autowire*/",
        "/*autoreg*/",
        "//}}}",
        "/*autofsm*/ // P0,P1,P2,P3 cur_sta nxt_sta --> use cmd AFM, P0-P3 is status, nxt_sta is not necessary ",
        "",
    ]


def _local_vars(pwd: str, vc: str) -> list[str]:
    out = [
        "// it's better to put file dir into file_dir.vc (-y dir0 -y dir1), but you can choose other method in menu-verilog-xxEmacs-- ",
        "// Local Variables:",
        "// verilog-auto-inst-param-value:t",
        f'// verilog-library-flags:("-y  {pwd}" ) ',
    ]
    if vc:
        out.append(f'// verilog-library-flags:("-f {vc}" ) ')
    out += [
        f'// verilog-library-directories:("{pwd}" ) ',
        "// End:",
        "",
    ]
    return out


def gen_template(
    filename: str,
    company: str,
    author: str,
    pwd: str,
    vc: str,
    now: datetime.datetime,
) -> list[str]:
    """A complete new file: header block + module skeleton + Local Variables.

    The module name is the first word of the basename; a name ending in
    ``tb`` gets the testbench skeleton, anything else the normal module one.
    """
    modulename = _module_name(filename)
    out = _header_lines(filename, company, author, now)
    out += ["", "//`timescale 1ns/1ps", "", f"module {modulename}(/*autoarg*/);"]
    if modulename.endswith("tb"):
        out += _tb_skeleton(modulename)
    else:
        out += _module_skeleton()
    out += _local_vars(pwd, vc)
    out += ["endmodule", ""]
    return out


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.fmt)


def _add_header_method(
    self: VerilogBuffer,
    filename: str = "",
    *,
    company: str | None = None,
    author: str | None = None,
    now: datetime.datetime | None = None,
    env: Mapping[str, str] | None = None,
) -> VerilogBuffer:
    """Prepend the ``// +FHDR`` header block; unset values come from ENV."""
    env = os.environ if env is None else env
    if company is None:
        company = get_company(env)
    if author is None:
        author = get_author(env)
    if now is None:
        now = datetime.datetime.now()
    return VerilogBuffer(add_header(self._lines, filename, company, author, now))


VerilogBuffer.add_header = _add_header_method


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.inst / verilog_tooling.arg so Vim can call it
# the same way)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.filehdr",
        description="automatic.vim AddHeader / AutoTemplate rewrite",
    )
    parser.add_argument(
        "command",
        choices=["template", "header"],
        help="template: create a new file skeleton; header: prepend the +FHDR block",
    )
    parser.add_argument(
        "-f", "--file", default=None, help="template: file to create"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="template: overwrite an existing file",
    )
    parser.add_argument(
        "-i", "--in_file", default=None, help="header: file to prepend the header to"
    )
    parser.add_argument(
        "-o",
        "--out_file",
        default=None,
        help="header: output file (default: in place)",
    )
    # accepted and ignored, so the shared Vim front-end can always pass them
    parser.add_argument("-y", "--libdir", action="append", default=[],
                        help="(unused by these commands)")
    parser.add_argument("--ref_file", default=None,
                        help="(unused by these commands)")
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    args = create_by_args(argv)
    env = os.environ
    company = get_company(env)
    author = get_author(env)
    now = datetime.datetime.now()
    if args.command == "template":
        if not args.file:
            raise SystemExit("template: -f/--file is required")
        path = Path(args.file)
        if path.exists() and not args.force:
            raise SystemExit(f"file exists: {path} (use --force to overwrite)")
        out = gen_template(args.file, company, author, get_pwd(env), get_vc(env), now)
        path.write_text("\n".join(out) + "\n")
    else:
        if not args.in_file:
            raise SystemExit("header: -i/--in_file is required")
        in_path = Path(args.in_file)
        lines = in_path.read_text().splitlines()
        out = add_header(lines, args.in_file, company, author, now)
        out_path = Path(args.out_file) if args.out_file else in_path
        out_path.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
