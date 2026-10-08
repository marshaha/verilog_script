"""verilog-mode AUTOINPUT / AUTOOUTPUT (verilog_tooling.inout).

Signal sets, exclusion rules, v2k (in-header) comma repairs, marker regexp
arguments, parameter substitution, idempotency and kill — cross-checked
against the real emacs verilog-mode where noted.
"""

from verilog_tooling.inout import (
    _INPUT_HEADER,
    _OUTPUT_HEADER,
    auto_input,
    auto_io,
    auto_output,
    kill_auto_input,
    kill_auto_output,
)
from verilog_tooling.inst import parse_module_ports

CLOSER = "// End of automatics"

SUB = """\
module sub (
    input  wire [7:0] din,
    input  wire       clk,
    output wire [3:0] dout,
    output wire       flag,
    inout  wire       io
);
endmodule
"""


def mods():
    return {"sub": parse_module_ports(SUB.splitlines())}


def decl(keyword: str, msb: str, name: str, comment: str = "", term: str = ";", indent: int = 0) -> str:
    """One generated declaration line, the autodef arithmetic (same house
    style as AUTOWIRE): keyword padded by CalMargin(12, len), '[msb:0]'
    unless scalar, CalMargin(39, len) before the name, comment at column
    max(48, indent+40)."""
    line = " " * indent + keyword + " " * (12 - len(keyword) + 1)
    if msb:
        line += f"[{msb}:0]"
    line += " " * (39 - len(line) + 1 + indent) + name + term
    if comment:
        col = max(48, indent + 40)
        line += " " * max(col - len(line), 1) + comment
    return line


TOP_BODY = """\
module top;
/*AUTOINPUT*/
/*AUTOOUTPUT*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""


# ---------------------------------------------------------------------------
# kill


def test_kill_regions_keep_markers_and_each_other():
    lines = [
        "/*AUTOINPUT*/",
        _INPUT_HEADER,
        "input a;",
        CLOSER,
        "/*AUTOOUTPUT*/",
        _OUTPUT_HEADER,
        "output b;",
        CLOSER,
    ]
    assert kill_auto_input(lines) == [
        "/*AUTOINPUT*/",
        "/*AUTOOUTPUT*/",
        _OUTPUT_HEADER,
        "output b;",
        CLOSER,
    ]
    assert kill_auto_output(lines) == [
        "/*AUTOINPUT*/",
        _INPUT_HEADER,
        "input a;",
        CLOSER,
        "/*AUTOOUTPUT*/",
    ]


def test_kill_output_spares_outputevery_region():
    lines = [
        "/*AUTOOUTPUTEVERY*/",
        "// Beginning of automatic outputs (every signal)",
        "output b;",
        CLOSER,
    ]
    assert kill_auto_output(lines) == lines


# ---------------------------------------------------------------------------
# signal sets (body placement, 1995 style)


def test_body_placement_basic():
    out = auto_io(TOP_BODY.splitlines(), mods())
    assert out == [
        "module top;",
        "/*AUTOINPUT*/",
        _INPUT_HEADER,
        decl("input", "", "clk", "// To u_sub of sub.v"),
        decl("input", "7", "din", "// To u_sub of sub.v"),
        CLOSER,
        "/*AUTOOUTPUT*/",
        _OUTPUT_HEADER,
        decl("output", "3", "dout_w", "// From u_sub of sub.v"),
        decl("output", "", "flag_w", "// From u_sub of sub.v"),
        CLOSER,
        "sub u_sub (/*autoinst*/",
        "    .clk  (clk),",
        "    .din  (din),",
        "    .dout (dout_w),",
        "    .flag (flag_w),",
        "    .io   (io_w)",
        ");",
        "endmodule",
    ]


def test_input_exclusions():
    """A net is NOT an input when it is a port, a declared wire, or driven
    by an instance output.  An UNDECLARED assign-driven net IS emitted
    (verilog-mode parity, probe6: promote it to a port and drop the assign
    yourself — verilator flags the intermediate state, as with emacs)."""
    top = """\
module top (input ex_port);
/*AUTOINPUT*/
wire declared_w;
assign assigned_w = 1'b1;
sub u1 (/*autoinst*/
    .clk  (clk),
    .din  (declared_w),
    .dout (mid),
    .flag (flag_w),
    .io   (io_w)
);
sub u2 (/*autoinst*/
    .clk  (ex_port),
    .din  (mid),
    .dout (d2),
    .flag (f2),
    .io   ()
);
sub u3 (/*autoinst*/
    .clk  (assigned_w),
    .din  (d3in),
    .dout (d3),
    .flag (f3),
    .io   ()
);
endmodule
"""
    out = auto_input(top.splitlines(), mods())
    inputs = [ln for ln in out if ln.startswith("input")]
    assert inputs == [
        decl("input", "", "assigned_w", "// To u3 of sub.v"),
        decl("input", "", "clk", "// To u1 of sub.v"),
        decl("input", "7", "d3in", "// To u3 of sub.v"),
    ]


def test_output_exclusions():
    """A net is NOT an output when it is already a port of this module or
    feeds an instance input/inout (internal — AUTOWIRE territory).  A
    hand-declared wire is NOT excluded (verilog-mode parity)."""
    top = """\
module top (output ex_out);
/*AUTOOUTPUT*/
wire declared_w;
sub u1 (/*autoinst*/
    .clk  (clk),
    .din  (mid),
    .dout (mid),
    .flag (declared_w),
    .io   (io_w)
);
sub u2 (/*autoinst*/
    .clk  (clk),
    .din  (din2),
    .dout (ex_out),
    .flag (flag2),
    .io   (io_w)
);
endmodule
"""
    out = auto_output(top.splitlines(), mods())
    outputs = [ln for ln in out if ln.startswith("output") and "module" not in ln]
    assert outputs == [
        decl("output", "", "declared_w", "// From u1 of sub.v"),
        decl("output", "", "flag2", "// From u2 of sub.v"),
    ]


def test_concat_and_expression_connections_skipped():
    top = """\
module top;
/*AUTOINPUT*/
sub u1 (/*autoinst*/
    .clk  ({clk_a, clk_b}),
    .din  (~din_n),
    .dout (dout_w),
    .flag (1'b0),
    .io   (io_w)
);
endmodule
"""
    out = auto_input(top.splitlines(), mods())
    assert _INPUT_HEADER not in out


def test_inout_nets_go_to_neither():
    out = auto_io(TOP_BODY.splitlines(), mods())
    assert not any("io_w" in ln for ln in out if ln.startswith(("input", "output")))


# ---------------------------------------------------------------------------
# v2k (in-header) placement


def test_v2k_after_existing_port():
    top = """\
module top (
    input ext_clk,
    /*AUTOINPUT*/
    /*AUTOOUTPUT*/
);
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""
    out = auto_io(top.splitlines(), mods())
    assert out == [
        "module top (",
        "    input ext_clk,",
        "    /*AUTOINPUT*/",
        "    " + _INPUT_HEADER,
        decl("input", "", "clk", "// To u_sub of sub.v", ",", indent=4),
        decl("input", "7", "din", "// To u_sub of sub.v", ",", indent=4),
        "    " + CLOSER,
        "    /*AUTOOUTPUT*/",
        "    " + _OUTPUT_HEADER,
        decl("output", "3", "dout_w", "// From u_sub of sub.v", ",", indent=4),
        decl("output", "", "flag_w", "// From u_sub of sub.v", "", indent=4),
        "    " + CLOSER,
        ");",
        "sub u_sub (/*autoinst*/",
        "    .clk  (clk),",
        "    .din  (din),",
        "    .dout (dout_w),",
        "    .flag (flag_w),",
        "    .io   (io_w)",
        ");",
        "endmodule",
    ]


def test_v2k_open_comma_added_after_previous_port():
    """verilog-repair-open-comma: a port before the marker without a comma
    gets one."""
    top = """\
module top (
    input ext_clk
    /*AUTOINPUT*/
);
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""
    out = auto_input(top.splitlines(), mods())
    assert out[1] == "    input ext_clk,"


def test_v2k_marker_immediately_after_open_paren():
    """verilog-mode errors out here (\"Mismatching ()\"); we expand normally
    and break the marker line so the region stays inside the header."""
    top = """\
module top (/*AUTOINPUT*/
            /*AUTOOUTPUT*/);
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""
    out = auto_io(top.splitlines(), mods())
    # valid structure: regions inside the header, `);` on its own line,
    # no dangling comma before it
    assert out[0] == "module top (/*AUTOINPUT*/"
    assert out[1] == _INPUT_HEADER
    assert out[3].endswith("din,    // To u_sub of sub.v")
    assert out[5] == "            /*AUTOOUTPUT*/"
    assert ");" in out
    close = out.index(");")
    assert out[close - 1] == "            " + CLOSER
    assert not out[close - 2].rstrip().endswith(",")


def test_v2k_idempotent_regeneration():
    top = """\
module top (
    input ext_clk,
    /*AUTOINPUT*/
    /*AUTOOUTPUT*/
);
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""
    once = auto_io(top.splitlines(), mods())
    assert auto_io(once, mods()) == once


def test_body_idempotent_regeneration():
    once = auto_io(TOP_BODY.splitlines(), mods())
    assert auto_io(once, mods()) == once


# ---------------------------------------------------------------------------
# marker regexp argument


def test_regexp_argument_filters_signals():
    top = """\
module top;
/*AUTOINPUT("^i_")*/
/*AUTOOUTPUT("?!^unused")*/
sub u1 (/*autoinst*/
    .clk  (i_clk),
    .din  (i_din),
    .dout (unused_dbg),
    .flag (flag_o),
    .io   ()
);
endmodule
"""
    out = auto_io(top.splitlines(), mods())
    inputs = [ln for ln in out if ln.startswith("input")]
    outputs = [ln for ln in out if ln.startswith("output")]
    assert len(inputs) == 2 and all("_i" in ln or "i_" in ln for ln in inputs)
    assert outputs == [decl("output", "", "flag_o", "// From u1 of sub.v")]


def test_ignore_regexp_file_local_variable():
    top = """\
module top;
/*AUTOINPUT*/
sub u1 (/*autoinst*/
    .clk  (i_clk),
    .din  (i_din),
    .dout (o1),
    .flag (o2),
    .io   ()
);
endmodule
// Local Variables:
// verilog-auto-input-ignore-regexp:"^i_clk$"
// End:
"""
    out = auto_input(top.splitlines(), mods())
    inputs = [ln for ln in out if ln.startswith("input")]
    assert inputs == [decl("input", "7", "i_din", "// To u1 of sub.v")]


# ---------------------------------------------------------------------------
# parameter substitution / multidim


def test_param_value_substitution_in_width():
    sub = """\
module subp #(
    parameter W = 8
)(
    input  wire [W-1:0] din,
    output wire [W-1:0] dout
);
endmodule
"""
    m = {"subp": parse_module_ports(sub.splitlines())}
    top = """\
module top;
/*AUTOINPUT*/
/*AUTOOUTPUT*/
subp #(.W(4)) u_s (/*autoinst*/
    .din  (din),
    .dout (dout)
);
endmodule
"""
    out = auto_io(top.splitlines(), m)
    assert decl("input", "3", "din", "// To u_s of subp.v") in out
    assert decl("output", "3", "dout", "// From u_s of subp.v") in out


def test_multidim_packed_port():
    sub = """\
module subm (
    input  wire [3:0][7:0] p2,
    output wire [1:0][3:0] q2
);
endmodule
"""
    m = {"subm": parse_module_ports(sub.splitlines())}
    top = """\
module top;
/*AUTOINPUT*/
/*AUTOOUTPUT*/
subm u_m (/*autoinst*/
    .p2 (p2_net/*[3:0][7:0]*/),
    .q2 (q2_net/*[1:0][3:0]*/)
);
endmodule
"""
    out = auto_io(top.splitlines(), m)
    in_lines = [ln for ln in out if ln.startswith("input")]
    out_lines = [ln for ln in out if ln.startswith("output")]
    assert in_lines[0].startswith("input        [3:0] [7:0]")
    assert "p2_net;" in in_lines[0]
    assert out_lines[0].startswith("output       [1:0] [3:0]")
    assert "q2_net;" in out_lines[0]


def test_foreign_width_promoted_at_pin_dimension():
    """A width naming symbols the parent does not define is declared as
    written (the pin's dimension), per the driver-dimension rule —
    supersedes the older skip-it policy."""
    sub = """\
module subf (
    input  wire [SUBW-1:0] din,
    output wire [SUBW-1:0] dout
);
endmodule
"""
    m = {"subf": parse_module_ports(sub.splitlines())}
    top = """\
module top;
/*AUTOINPUT*/
/*AUTOOUTPUT*/
subf u_f (/*autoinst*/
    .din  (din),
    .dout (dout)
);
endmodule
"""
    out = auto_io(top.splitlines(), m)
    text = "\n".join(out)
    assert _INPUT_HEADER in out
    assert _OUTPUT_HEADER in out
    assert "din" in text and "[SUBW-1:0]" in text


# ---------------------------------------------------------------------------
# markers without signals / without module files


def test_no_marker_no_change():
    lines = ["module top;", "endmodule"]
    assert auto_io(lines, mods()) == lines


def test_missing_module_file_no_crash():
    assert auto_io(TOP_BODY.splitlines(), {}) == TOP_BODY.splitlines()


def test_second_marker_gets_own_region():
    """Each marker expands independently (a filtered split across two
    markers is the supported use; overlapping filters duplicate decls, as
    in emacs)."""
    top = """\
module top;
/*AUTOINPUT("^i_")*/
/*AUTOINPUT("^c")*/
sub u1 (/*autoinst*/
    .clk  (c_clk),
    .din  (i_din),
    .dout (o1),
    .flag (o2),
    .io   ()
);
endmodule
"""
    out = auto_input(top.splitlines(), mods())
    regions = [i for i, ln in enumerate(out) if ln == _INPUT_HEADER]
    assert len(regions) == 2
    assert out[regions[0] + 1].startswith("input") and "i_din" in out[regions[0] + 1]
    assert out[regions[1] + 1].startswith("input") and "c_clk" in out[regions[1] + 1]


# ---------------------------------------------------------------------------
# verilog-auto-ignore-concat / verilog-auto-wire-comment (file-local vars)

IGNORE_NIL = """\
module top;
/*AUTOINPUT*/
/*AUTOOUTPUT*/
sub u1 (/*autoinst*/
    .clk  ((clk_x)),
    .din  ({sig_a, sig_b[3:0], 1'b0, `MACRO, {nest_c, nest_d}}),
    .dout ({x_w, y_w}),
    .flag (flag_w),
    .io   ()
);
endmodule
// Local Variables:
// verilog-auto-ignore-concat: nil
// End:
"""


def test_ignore_concat_default_skips_braces():
    """Default (our house default = t): {...}/(...) connections are ignored —
    the user's '{}' exemption workflow."""
    top = """\
module top;
/*AUTOINPUT*/
sub u1 (/*autoinst*/
    .clk  (clk),
    .din  ({sig_a, sig_b}),
    .dout (dout_w),
    .flag (flag_w),
    .io   ()
);
endmodule
"""
    out = auto_input(top.splitlines(), mods())
    inputs = [ln for ln in out if ln.startswith("input")]
    assert inputs == [decl("input", "", "clk", "// To u1 of sub.v")]


def test_ignore_concat_nil_extracts_signals():
    out = auto_io(IGNORE_NIL.splitlines(), mods())
    inputs = [ln for ln in out if ln.startswith("input")]
    outputs = [ln for ln in out if ln.startswith("output")]
    # clk_x via parens; sig_a scalar, sig_b from its own [3:0]; nested
    # nest_c/nest_d; the literal and `MACRO are dropped
    assert inputs == [
        decl("input", "", "clk_x", "// To u1 of sub.v"),
        decl("input", "", "nest_c", "// To u1 of sub.v"),
        decl("input", "", "nest_d", "// To u1 of sub.v"),
        decl("input", "", "sig_a", "// To u1 of sub.v"),
        decl("input", "3", "sig_b", "// To u1 of sub.v"),
    ]
    assert outputs == [
        decl("output", "", "flag_w", "// From u1 of sub.v"),
        decl("output", "", "x_w", "// From u1 of sub.v"),
        decl("output", "", "y_w", "// From u1 of sub.v"),
    ]


def test_wire_comment_nil_suppresses_comments():
    top = IGNORE_NIL.replace(
        "verilog-auto-ignore-concat: nil", "verilog-auto-wire-comment: nil"
    )
    out = auto_io(top.splitlines(), mods())
    decls = [ln for ln in out if ln.startswith(("input", "output"))]
    assert decls  # declarations still generated
    assert not any("// To " in ln or "// From " in ln for ln in decls)


# ---------------------------------------------------------------------------
# AUTOINOUT (verilog-auto-inout)

from verilog_tooling.inout import _INOUT_HEADER, auto_inout, kill_auto_inout

TOP_INOUT = """\
module top;
/*AUTOINOUT*/
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""


def test_autoinout_basic():
    out = auto_inout(TOP_INOUT.splitlines(), mods())
    assert out[1] == "/*AUTOINOUT*/"
    assert out[2] == _INOUT_HEADER
    assert out[3] == decl("inout", "", "io_w", "// To/From u_sub of sub.v")
    assert out[4] == CLOSER


def test_autoinout_exclusions():
    """A net on an instance inout is NOT made an inout port when it is a
    module port already, or is also seen on an instance input/output."""
    top = """\
module top (inout ex_io);
/*AUTOINOUT*/
sub u1 (/*autoinst*/
    .clk  (clk),
    .din  (shared_in),
    .dout (shared_out),
    .flag (flag_w),
    .io   (io_w)
);
sub u2 (/*autoinst*/
    .clk  (clk),
    .din  (io_w),
    .dout (d2),
    .flag (f2),
    .io   (ex_io)
);
sub u3 (/*autoinst*/
    .clk  (clk),
    .din  (d3),
    .dout (shared_out),
    .flag (f3),
    .io   (shared_in)
);
endmodule
"""
    out = auto_inout(top.splitlines(), mods())
    inouts = [ln for ln in out if ln.startswith("inout")]
    # io_w feeds u2's input; shared_out is u1's output; ex_io is a port;
    # shared_in is u1's input (even though also on u3's inout)
    assert inouts == []


def test_autoinout_v2k_and_regexp():
    top = """\
module top (
    /*AUTOINOUT("^io_")*/
);
sub u_sub (/*autoinst*/
    .clk  (clk),
    .din  (din),
    .dout (dout_w),
    .flag (flag_w),
    .io   (io_w)
);
endmodule
"""
    out = auto_inout(top.splitlines(), mods())
    inouts = [ln for ln in out if ln.strip().startswith("inout")]
    assert inouts == [
        decl("inout", "", "io_w", "// To/From u_sub of sub.v", "", indent=4)
    ]
    # no dangling comma before the header close
    assert out[out.index(");") - 1] == "    " + CLOSER


def test_kill_auto_inout():
    lines = ["/*AUTOINOUT*/", _INOUT_HEADER, "inout a;", CLOSER]
    assert kill_auto_inout(lines) == ["/*AUTOINOUT*/"]


def test_output_promoted_with_symbolic_driver_dimension():
    modc = """\
module modc #(parameter W = D*K, parameter D = 4) (
    input  wire         clk,
    output wire [W-1:0] dout
);
endmodule
"""
    m = {"modc": parse_module_ports(modc.splitlines(), with_params=True)}
    top = """\
module top;
/*AUTOOUTPUT*/
modc u_a (/*autoinst*/
    .clk  (clk),
    .dout (mid)
);
endmodule
"""
    out = auto_output(top.splitlines(), m)
    text = "\n".join(out)
    # D folds to 4, K stays symbolic: the net is promoted at the
    # driver's dimension instead of being silently skipped
    (line,) = [ln for ln in out if ln.startswith("output") and "mid" in ln]
    assert "K" in line
