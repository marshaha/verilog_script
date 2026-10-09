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
    # localparams are never AUTOINSTPARAM parameters (verilog-decls-get-gparams
    # collects `parameter` only — a localparam cannot be overridden)
    params = parse_module_params(lines)
    assert [p.name for p in params] == []
    paras = get_all_paras(lines)
    assert {"W", "DEPTH", "PTR", "PAYLD"} <= paras
    consts = _const_symbols(lines)
    assert consts["W"] == 8 and consts["DEPTH"] == 4
    assert consts["PTR"] == 2  # ternary folded: (4==1) ? 1 : $clog2(4)
    assert consts["PAYLD"] == 15  # arithmetic folded: 8+4+3
    set_include_dirs([])


def test_filelist_file_dirs_join_include_path(tmp_path):
    """A header present ONLY as a -f filelist FILE entry must be
    findable by `include: _cli_resolve puts the listed files'
    directories on the include search path.  Regression: such headers
    failed to resolve, so every parameter they defined stayed
    invisible (param names reported as unresolved signals; widths
    naming them dropped as not-visible-here)."""
    from types import SimpleNamespace

    from verilog_tooling.autodef import get_all_paras
    from verilog_tooling.inst import _cli_resolve
    from verilog_tooling.libdirs import expand_includes, include_dirs, set_include_dirs

    rtl = tmp_path / "rtl"
    inc = tmp_path / "inc"
    rtl.mkdir()
    inc.mkdir()
    (inc / "p.svh").write_text("parameter P_WIDTH = 32,\n")
    (tmp_path / "files.f").write_text("inc/p.svh\n")
    top = rtl / "top.v"
    top.write_text(
        'module chip_top #(\n  `include "p.svh"\n) (input wire clk);\n'
        "endmodule\n\n"
        "// Local Variables:\n"
        '// verilog-library-flags: ("-f ../files.f")\n'
        "// End:\n"
    )
    lines = top.read_text().splitlines()
    args = SimpleNamespace(libdir=[], in_file=str(top), ref_file=None)
    _cli_resolve(args, lines)
    try:
        assert str(inc) in [str(d) for d in include_dirs()]
        assert any("parameter P_WIDTH" in ln for ln in expand_includes(lines))
        assert "P_WIDTH" in get_all_paras(lines)
    finally:
        set_include_dirs([])


def test_filelist_file_suffix_match_for_subpath_include(tmp_path):
    """`` `include "defs/p.svh"`` resolves when the filelist lists the
    header as ``inc/defs/p.svh`` (listed under a different root): the
    include name matches the listed file by path suffix."""
    from types import SimpleNamespace

    from verilog_tooling.autodef import get_all_paras
    from verilog_tooling.inst import _cli_resolve
    from verilog_tooling.libdirs import expand_includes, set_include_dirs

    rtl = tmp_path / "rtl"
    defs = tmp_path / "inc" / "defs"
    rtl.mkdir(parents=True)
    defs.mkdir(parents=True)
    (defs / "p.svh").write_text("parameter P_WIDTH = 32,\n")
    (tmp_path / "files.f").write_text("inc/defs/p.svh\n")
    top = rtl / "top.v"
    top.write_text(
        'module chip_top #(\n  `include "defs/p.svh"\n) (input wire clk);\n'
        "endmodule\n\n"
        "// Local Variables:\n"
        '// verilog-library-flags: ("-f ../files.f")\n'
        "// End:\n"
    )
    lines = top.read_text().splitlines()
    args = SimpleNamespace(libdir=[], in_file=str(top), ref_file=None)
    _cli_resolve(args, lines)
    try:
        assert any("parameter P_WIDTH" in ln for ln in expand_includes(lines))
        assert "P_WIDTH" in get_all_paras(lines)
    finally:
        set_include_dirs([])


def test_chip_top_include_params_through_adt_cli(tmp_path):
    """End-to-end adoption case: chip_top takes its parameter block
    from `include headers that exist ONLY on a -f filelist (one under
    a defs/ subpath, listed from a different root).  Before the fix,
    adt reported every header parameter as an unresolved signal and
    dropped assign-propagated widths naming them; after it, the run
    is warning-free with no unresolved lines."""
    from verilog_tooling import autodef

    rtl = tmp_path / "rtl"
    defs = tmp_path / "inc" / "defs"
    inc = tmp_path / "inc"
    rtl.mkdir(parents=True)
    defs.mkdir(parents=True)
    inc.mkdir(parents=True, exist_ok=True)
    (defs / "fab_cpu_params.svh").write_text(
        "parameter FAB_CPU_DATA_PTR_WIDTH_AR = 32,\n"
        "parameter FAB_CPU_DATA_PTR_WIDTH_AW = 32,\n"
        "parameter FAB_CPU_DATA_PTR_WIDTH_B  = 32,\n"
    )
    (inc / "fab_periph_params.svh").write_text(
        "parameter FAB_PERIPH_MM_DATA_PTR_WIDTH_R = 16,\n"
    )
    (rtl / "sub.v").write_text(
        "module sub #(parameter W = 8) (\n"
        "    input  wire         clk,\n"
        "    output wire [W-1:0] ar_data,\n"
        "    output wire [W-1:0] aw_data,\n"
        "    output wire [W-1:0] b_data\n"
        ");\n"
        "endmodule\n"
    )
    (tmp_path / "files.f").write_text(
        "inc/defs/fab_cpu_params.svh\ninc/fab_periph_params.svh\nrtl/sub.v\n"
    )
    top = rtl / "chip_top.v"
    top.write_text(
        "module chip_top #(\n"
        '  `include "defs/fab_cpu_params.svh",\n'
        '  `include "fab_periph_params.svh"\n'
        ") (\n"
        "    input wire clk\n"
        ");\n"
        "/*autodef*/\n"
        "sub #(.W(FAB_CPU_DATA_PTR_WIDTH_AR)) u_ar (\n"
        "    .clk     (clk),\n"
        "    .ar_data (fab_cpu_periph_rd_addr_ar_data)\n"
        ");\n"
        "sub #(.W(FAB_CPU_DATA_PTR_WIDTH_AW)) u_aw (\n"
        "    .clk     (clk),\n"
        "    .aw_data (fab_cpu_periph_rd_addr_aw_data)\n"
        ");\n"
        "sub #(.W(FAB_CPU_DATA_PTR_WIDTH_B)) u_b (\n"
        "    .clk    (clk),\n"
        "    .b_data (b_raw)\n"
        ");\n"
        "assign fab_cpu_periph_rd_addr_b_data = b_raw;\n"
        "endmodule\n"
        "\n"
        "// Local Variables:\n"
        '// verilog-library-flags: ("-f ../files.f")\n'
        "// End:\n"
    )
    out = tmp_path / "chip_out.v"
    autodef.main(
        ["adt", "-i", str(top), "-o", str(out), "--ref_file", str(top), "-y", str(rtl)]
    )
    text = out.read_text()
    assert "unresolved" not in text
    for sig in (
        "fab_cpu_periph_rd_addr_ar_data;",
        "fab_cpu_periph_rd_addr_aw_data;",
        "fab_cpu_periph_rd_addr_b_data;",
    ):
        assert sig in text
    assert "(FAB_CPU_DATA_PTR_WIDTH_B)-1:0" in text


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


def test_localparam_entries_not_params():
    """#(localparam X = 1, ...) entries are NOT parameters: verilog-mode's
    verilog-decls-get-gparams collects `parameter` only — a localparam
    cannot be overridden, so AUTOINSTPARAM must not emit .X(X) for it
    (emacs-verified on a #(parameter/localparam mix) submodule)."""
    from verilog_tooling.emacs import parse_module_params

    lines = ["module m #(localparam X = 1, parameter Y = 2) (input a);"]
    assert [p.name for p in parse_module_params(lines)] == ["Y"]


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


# ---------------------------------------------------------------------------
# field-debug log


def test_dbg_silent_without_env_and_loud_with_it(monkeypatch, capsys):
    from verilog_tooling.libdirs import _dbg

    monkeypatch.delenv("VERILOG_TOOLING_DEBUG", raising=False)
    _dbg("marker-off")
    assert "marker-off" not in capsys.readouterr().err
    monkeypatch.setenv("VERILOG_TOOLING_DEBUG", "1")
    _dbg("marker-on")
    err = capsys.readouterr().err
    assert "[verilog_tooling:debug] marker-on" in err


def test_find_include_logs_resolution_once(monkeypatch, tmp_path, capsys):
    from verilog_tooling.libdirs import _find_include, set_include_dirs

    (tmp_path / "p.svh").write_text("parameter W = 8,\n")
    set_include_dirs([str(tmp_path)])
    try:
        monkeypatch.setenv("VERILOG_TOOLING_DEBUG", "1")
        assert _find_include("p.svh") is not None
        _find_include("p.svh")  # second ask is cached: no second line
        err = capsys.readouterr().err
        assert err.count("include p.svh ->") == 1
        monkeypatch.delenv("VERILOG_TOOLING_DEBUG")
        assert _find_include("missing_xyz.svh") is None
        assert "missing_xyz" not in capsys.readouterr().err
    finally:
        set_include_dirs([])


def test_cli_resolve_debug_summary(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from verilog_tooling.inst import _cli_resolve

    (tmp_path / "files.f").write_text("top.v\n")
    top = tmp_path / "top.v"
    top.write_text(
        "module top; endmodule\n\n"
        "// Local Variables:\n"
        '// verilog-library-flags: ("-f ./files.f")\n'
        "// End:\n"
    )
    from verilog_tooling.libdirs import set_filelist_files

    args = SimpleNamespace(libdir=[], in_file=str(top), ref_file=None)
    lines = top.read_text().splitlines()
    monkeypatch.setenv("VERILOG_TOOLING_DEBUG", "1")
    try:
        _cli_resolve(args, lines)
    finally:
        set_filelist_files([])
    err = capsys.readouterr().err
    assert "filelist:" in err and "files.f (read)" in err
    assert "module libdirs:" in err
    assert "include dirs:" in err
