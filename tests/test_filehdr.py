"""automatic.vim AddHeader / AutoTemplate rewritten in Python (filehdr.py)."""

import datetime

import pytest

import verilog_tooling.filehdr  # noqa: F401  (attaches VerilogBuffer.add_header)
from verilog_tooling.filehdr import (
    add_header,
    gen_template,
    get_author,
    get_company,
    get_pwd,
    get_vc,
    main,
)
from verilog_tooling.inst import VerilogBuffer

NOW = datetime.datetime(2024, 3, 5, 9, 30)

HEADER = [
    "// +FHDR----------------------------------------------------------------------",
    "//                 Copyright (c) 2024 ACME.",
    "//                     ALL RIGHTS RESERVED",
    "//  This source file is the property of ACME  Technology Co., Ltd. and",
    "//  may not be copied or distributed in any isomorphic form without the prior",
    "//  written consent of ACME Technology Co., Ltd.",
    "// ---------------------------------------------------------------------------",
    "// Filename      : foo.v",
    "// Author        : mars",
    "// Created On    : 2024-03-05 09:30",
    "// Last Modified : ",
    "// ---------------------------------------------------------------------------",
    "// Description:",
    "//",
    "//",
    "// -FHDR----------------------------------------------------------------------",
    "",
]


# ---------------------------------------------------------------------------
# add_header (AddHeader)


def test_add_header_prepends_block_with_exact_strings():
    out = add_header(["module foo;"], "foo.v", "ACME", "mars", NOW)
    assert out[: len(HEADER)] == HEADER
    assert out[len(HEADER) :] == ["module foo;"]


def test_add_header_is_idempotent():
    once = add_header(["module foo;"], "foo.v", "ACME", "mars", NOW)
    assert add_header(once, "foo.v", "OTHER", "bob", NOW) == once


# ---------------------------------------------------------------------------
# gen_template (AutoTemplate)


def test_gen_template_normal_module():
    out = gen_template("foo.v", "ACME", "mars", "/work/rtl", "", NOW)
    assert out[: len(HEADER)] == HEADER
    assert "//`timescale 1ns/1ps" in out
    assert "module foo(/*autoarg*/);" in out
    assert "/*autoDISABLEinput*/" in out
    assert "/*autoDISABLEoutput*/" in out
    assert "input                                   clk;" in out
    assert "input                                   rst_n;" in out
    assert "input                                   vld;" in out
    assert "input        [7:0]                      data;" in out
    assert "output                                  ack;" in out
    for marker in ("//{{{", "/*autodef*/", "/*autowire*/", "/*autoreg*/", "//}}}"):
        assert marker in out
    assert any(line.startswith("/*autofsm*/ // P0,P1,P2,P3") for line in out)
    assert "// Local Variables:" in out
    assert '// verilog-library-flags:("-y  /work/rtl" ) ' in out
    assert '// verilog-library-directories:("/work/rtl" ) ' in out
    assert out[-2:] == ["endmodule", ""]
    assert not any("autoreginput" in line for line in out)


def test_gen_template_testbench():
    out = gen_template("foo_tb.v", "ACME", "mars", "/work/rtl", "", NOW)
    assert "module foo_tb(/*autoarg*/);" in out
    assert "/*autoreginput*/" in out
    assert "/*autodef off*/" in out
    assert "    clk = 1'b0;" in out
    assert "    forever #10 clk = ~clk;" in out
    assert "    rst_n = 1'b0;" in out
    assert "    #52 rst_n = 1'b1;" in out
    assert '    $fsdbDumpfile("main.fsdb") ;' in out
    assert '    $fsdbDumpvars(0,foo_tb,"+mda");' in out
    assert "    #1000;" in out
    assert "    $finish;" in out
    assert "/*autodef on*/" in out
    assert "//inst u_inst(/*autoinst*/);" in out
    assert "// Local Variables:" in out
    assert out[-2:] == ["endmodule", ""]
    assert not any("autoDISABLEinput" in line for line in out)


def test_gen_template_vc_flags_line():
    with_vc = gen_template("foo.v", "ACME", "mars", "/work/rtl", "/work/files.vc", NOW)
    assert '// verilog-library-flags:("-f /work/files.vc" ) ' in with_vc
    without_vc = gen_template("foo.v", "ACME", "mars", "/work/rtl", "", NOW)
    assert not any('"-f ' in line for line in without_vc)


# ---------------------------------------------------------------------------
# environment helpers


def test_env_helpers():
    assert get_company({}) == ""
    assert get_company({"COMPANY": "ACME"}) == "ACME"
    assert get_author({"USER": "mars"}) == "mars"
    assert get_author({"USER": "mars", "USER_DIT": "dit"}) == "dit"
    assert get_author({"USER": "mars", "USER_DIT": "USER_DIT:x"}) == "mars"
    assert get_vc({}) == ""
    assert get_vc({"EMACS_VC": "a.vc"}) == "a.vc"
    assert get_vc({"EMACS_VC": "EMACS_VC:x"}) == ""
    assert get_pwd({"PWD": "/x"}) == "/x"


# ---------------------------------------------------------------------------
# VerilogBuffer method


def test_verilog_buffer_add_header():
    buf = VerilogBuffer(["module foo;"])
    out = buf.add_header("foo.v", company="ACME", author="mars", now=NOW)
    assert out.lines[: len(HEADER)] == HEADER
    assert out.lines[-1] == "module foo;"


# ---------------------------------------------------------------------------
# CLI


def test_cli_template_writes_new_file(tmp_path):
    target = tmp_path / "foo.v"
    main(["template", "-f", str(target)])
    lines = target.read_text().splitlines()
    assert lines[0].startswith("// +FHDR")
    assert "module foo(/*autoarg*/);" in lines


def test_cli_template_refuses_existing_without_force(tmp_path):
    target = tmp_path / "foo.v"
    target.write_text("module foo;\n")
    with pytest.raises(SystemExit):
        main(["template", "-f", str(target)])
    main(["template", "-f", str(target), "--force"])
    assert "module foo(/*autoarg*/);" in target.read_text().splitlines()


def test_cli_header_prepends_and_is_idempotent(tmp_path):
    buf = tmp_path / "foo.v"
    buf.write_text("module foo;\nendmodule\n")
    main(["header", "-i", str(buf)])
    first = buf.read_text()
    assert first.startswith("// +FHDR")
    assert first.splitlines()[-2:] == ["module foo;", "endmodule"]
    main(["header", "-i", str(buf)])
    assert buf.read_text() == first


def test_cli_header_out_file_leaves_input(tmp_path):
    buf = tmp_path / "foo.v"
    buf.write_text("module foo;\n")
    out_file = tmp_path / "out.v"
    main(["header", "-i", str(buf), "-o", str(out_file)])
    assert out_file.read_text().startswith("// +FHDR")
    assert buf.read_text() == "module foo;\n"


def test_skeleton_has_disabled_inout_marker():
    from verilog_tooling.filehdr import _module_skeleton

    out = "\n".join(_module_skeleton())
    assert "/*autoDISABLEinout*/" in out
