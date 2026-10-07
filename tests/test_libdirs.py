"""verilog-mode Local Variables parsing for module-file resolution (libdirs.py)."""

import os

from verilog_tooling.inst import _resolve_module_files, main
from verilog_tooling.libdirs import (
    parse_local_variables,
    read_vc_file,
    resolve_libdirs,
)

ENV = {"HOME": "/home/u", "PROJ_ROOT": "/proj"}

SMALL = """\
module small (
    input  wire       clk,
    input  wire [7:0] din,
    output wire       vld
);
endmodule
"""

# automatic.vim alignment: prefix floor 26, suffix floor 30+12=42.
CLK_LINE = "    .clk" + " " * 24 + "(clk" + " " * 40 + "), // input "
DIN_LINE = "    .din" + " " * 24 + "(din[7:0]" + " " * 35 + "), // input "
VLD_LINE_LAST = "    .vld" + " " * 24 + "(vld" + " " * 40 + ")  // output"


# ---------------------------------------------------------------------------
# verilog-library-directories


def test_parse_library_directories(tmp_path):
    file_dir = str(tmp_path)
    lines = [
        '// verilog-library-directories:("../pmu/" )',
        '// verilog-library-directories:("/abs/lib" "$PROJ_ROOT/rtl" "~/ip" )',
        "-y not_a_local_variable",  # ignored: not a // verilog-... line
    ]
    out = parse_local_variables(lines, file_dir, env=ENV)
    assert out["dirs"] == [
        os.path.normpath(os.path.join(file_dir, "../pmu")),
        "/abs/lib",
        "/proj/rtl",
        "/home/u/ip",
    ]
    assert out["vc_files"] == []
    assert out["inst_files"] == {}


def test_relative_dirs_resolve_against_file_dir_not_cwd(tmp_path, monkeypatch):
    buf_dir = tmp_path / "buf"
    buf_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    lines = ['// verilog-library-directories:("lib" "." )']
    out = parse_local_variables(lines, str(buf_dir), env=ENV)
    assert out["dirs"] == [str(buf_dir / "lib"), str(buf_dir)]


# ---------------------------------------------------------------------------
# verilog-library-flags / -f vc filelists


def test_parse_library_flags(tmp_path):
    file_dir = str(tmp_path)
    lines = [
        '// verilog-library-flags:("-y ../foo" "-f filelist.vc" )',
        '// verilog-library-flags:("-y /abs/y" "-y $PROJ_ROOT/lib" )',
    ]
    out = parse_local_variables(lines, file_dir, env=ENV)
    assert out["dirs"] == [
        os.path.normpath(os.path.join(file_dir, "../foo")),
        "/abs/y",
        "/proj/lib",
    ]
    assert out["vc_files"] == [os.path.join(file_dir, "filelist.vc")]


def test_read_vc_file(tmp_path):
    vc = tmp_path / "filelist.vc"
    vc.write_text(
        "# a comment\n"
        "// another comment\n"
        "\n"
        "   \n"
        "lib/a.v\n"
        "sub/dir\n"
        "$PROJ_ROOT/b.v\n"
        "~/c.v\n"
        "/abs/d.v\n"
    )
    assert read_vc_file(str(vc), env=ENV) == {
        "dirs": [],
        "files": [
            str(tmp_path / "lib" / "a.v"),
            str(tmp_path / "sub" / "dir"),
            "/proj/b.v",
            "/home/u/c.v",
            "/abs/d.v",
        ],
        "extensions": [],
    }


# ---------------------------------------------------------------------------
# verilog-inst-file


def test_parse_inst_file(tmp_path):
    file_dir = str(tmp_path)
    lines = [
        "// verilog-inst-file:../special/widgets.v",
        '// verilog-inst-file:("$PROJ_ROOT/ip/mod.sv")',
    ]
    out = parse_local_variables(lines, file_dir, env=ENV)
    assert out["inst_files"] == {
        "widgets": os.path.normpath(os.path.join(file_dir, "../special/widgets.v")),
        "mod": "/proj/ip/mod.sv",
    }
    assert out["dirs"] == []


# ---------------------------------------------------------------------------
# resolve_libdirs


def test_resolve_libdirs_extra_first_deduped(tmp_path):
    file_dir = str(tmp_path)
    lines = [
        '// verilog-library-directories:("./a" "../b" )',
        '// verilog-library-flags:("-y ./a" )',  # dup of ./a once resolved
    ]
    out = resolve_libdirs(lines, file_dir, extra_dirs=["/cli/dir", "/cli/dir"], env=ENV)
    assert out == [
        "/cli/dir",
        os.path.join(file_dir, "a"),
        os.path.normpath(os.path.join(file_dir, "../b")),
        # the buffer's own directory is always searched last (verilog-mode's
        # verilog-library-directories defaults to ("."): same-dir submodules)
        file_dir,
    ]


def test_resolve_libdirs_no_local_variables(tmp_path):
    # with no configuration the file's own directory is the search path
    assert resolve_libdirs(["module m; endmodule"], str(tmp_path), env=ENV) == [str(tmp_path)]
    assert resolve_libdirs(
        ["module m; endmodule"], str(tmp_path), extra_dirs=["."], env=ENV
    ) == [".", str(tmp_path)]


# ---------------------------------------------------------------------------
# end-to-end: Local Variables drive module-file resolution


def test_resolve_module_files_via_resolve_libdirs(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "small.v").write_text(SMALL)
    buf_dir = tmp_path / "buf"
    buf_dir.mkdir()
    lines = ['// verilog-library-directories:("../lib" )']
    libdirs = resolve_libdirs(lines, str(buf_dir), env=ENV)
    files = _resolve_module_files(["small"], libdirs)
    assert files == {"small": lib / "small.v"}


def test_cli_ait_uses_local_variables(tmp_path, capsys):
    # `ait` delegates to the emacs (eai) expansion and warns; the buffer's
    # Local Variables still locate the module file with no -y at all.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "small.v").write_text(SMALL)
    buf_dir = tmp_path / "buf"
    buf_dir.mkdir()
    buf = buf_dir / "top.v"
    buf.write_text(
        "small u_s (/*autoinst*/);\n"
        "\n"
        "// Local Variables:\n"
        '// verilog-library-directories:("../lib" )\n'
    )
    out_file = tmp_path / "out.v"
    # no -y at all: only the buffer's Local Variables can locate small.v
    main(["ait", "-i", str(buf), "-o", str(out_file)])
    text = out_file.read_text()
    assert ".clk" in text and ".din" in text and ".vld" in text
    assert "// Local Variables:" in text  # buffer tail preserved
    assert "deprecated" in capsys.readouterr().err


def test_cli_eai_uses_local_variables(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "small.v").write_text(SMALL)
    buf_dir = tmp_path / "buf"
    buf_dir.mkdir()
    buf = buf_dir / "top.v"
    buf.write_text(
        "small u_s (/*AUTOINST*/);\n"
        "\n"
        "// Local Variables:\n"
        '// verilog-library-flags:("-y ../lib" )\n'
    )
    out_file = tmp_path / "out.v"
    main(["eai", "-i", str(buf), "-o", str(out_file)])
    text = out_file.read_text()
    assert ".clk" in text and ".din" in text and ".vld" in text


# ---------------------------------------------------------------------------
# `include expansion


def test_expand_includes_inline_param_block(tmp_path):
    """`include may sit mid-line (#(`include "p.svh")); parameter collection
    sees through it (analysis only)."""
    from verilog_tooling.libdirs import set_include_dirs, expand_includes
    from verilog_tooling.emacs import parse_module_params
    from verilog_tooling.autodef import get_all_paras, _const_symbols

    (tmp_path / "m_params.svh").write_text(
        "localparam W = 8,\n"
        "localparam DEPTH = 4,\n"
        "localparam PTR = (DEPTH == 1) ? 1 : $clog2(DEPTH),\n"
        "localparam PAYLD = W+DEPTH+3\n"
    )
    set_include_dirs([str(tmp_path)])
    lines = ['module m #(`include "m_params.svh") (', "input clk);", "endmodule"]
    expanded = expand_includes(lines)
    assert any("localparam W" in ln for ln in expanded)
    assert any(ln.strip() == "module m #(" for ln in expanded)  # prefix kept
    params = parse_module_params(lines)
    assert [p.name for p in params] == ["W", "DEPTH", "PTR", "PAYLD"]
    paras = get_all_paras(lines)
    assert {"W", "DEPTH", "PTR", "PAYLD"} <= paras
    consts = _const_symbols(lines)
    assert consts["W"] == 8 and consts["DEPTH"] == 4
    assert consts["PTR"] == 2  # ternary folded: (4==1) ? 1 : $clog2(4)
    assert consts["PAYLD"] == 15  # arithmetic folded: 8+4+3
    set_include_dirs([])


def test_expand_includes_missing_keeps_line(tmp_path, capsys):
    from verilog_tooling.libdirs import set_include_dirs, expand_includes

    set_include_dirs([str(tmp_path)])
    lines = ['`include "nope.svh"']
    assert expand_includes(lines) == lines
    assert "include file not found" in capsys.readouterr().err
    set_include_dirs([])


def test_expand_includes_cycle_cut(tmp_path):
    from verilog_tooling.libdirs import set_include_dirs, expand_includes

    (tmp_path / "a.svh").write_text('`include "b.svh"\nlocalparam A = 1\n')
    (tmp_path / "b.svh").write_text('`include "a.svh"\nlocalparam B = 2\n')
    set_include_dirs([str(tmp_path)])
    out = expand_includes(['`include "a.svh"'])
    assert sum("localparam A" in ln for ln in out) == 1  # expanded exactly once
    assert sum("localparam B" in ln for ln in out) == 1
    set_include_dirs([])


def test_localparam_entries_parse_as_params():
    """#(localparam X = 1, ...) entries are parameters too."""
    from verilog_tooling.emacs import parse_module_params

    lines = ["module m #(localparam X = 1, parameter Y = 2) (input a);"]
    assert [p.name for p in parse_module_params(lines)] == ["X", "Y"]


def test_parse_module_ports_sees_through_include(tmp_path):
    """Module port parsing expands `include: a port list from an included
    header parses, and `define widths from the include are visible."""
    from verilog_tooling.libdirs import set_include_dirs
    from verilog_tooling.inst import parse_module_ports, find_interfaces

    (tmp_path / "defs.svh").write_text("`define DW 16\n")
    (tmp_path / "ports.svh").write_text(
        "input [`DW-1:0] a,\noutput [`DW-1:0] b\n"
    )
    (tmp_path / "iface.svh").write_text("interface hidden_if; logic x; endinterface\n")
    (tmp_path / "user.sv").write_text('`include "iface.svh"\nmodule u; endmodule\n')
    set_include_dirs([str(tmp_path)])
    lines = [
        'module m #(`include "defs.svh") (',
        '`include "ports.svh"',
        ");",
        "endmodule",
    ]
    md = parse_module_ports(lines)
    assert [p.name for p in md.ports if p.direction in ("input", "output")] == ["a", "b"]
    assert md.ports[0].width == "`DW-1:0"
    # interface declared inside an included header: .svh files are scanned
    # directly, and include edges to oddly-named include files (defs.inc
    # below) are followed by default; verilog-auto-read-includes:nil opts out
    ifaces = find_interfaces([str(tmp_path)])
    assert "hidden_if" in ifaces
    (tmp_path / "defs.inc").write_text("interface odd_if; logic y; endinterface\n")
    (tmp_path / "user2.sv").write_text('`include "defs.inc"\nmodule u2; endmodule\n')
    ifaces = find_interfaces([str(tmp_path)])
    assert "odd_if" in ifaces
    ifaces = find_interfaces([str(tmp_path)], read_includes=False)
    assert "odd_if" not in ifaces
    set_include_dirs([])
