"""Week 4: automatic.vim instance commands rewritten in Python (inst.py)."""

import pytest

from verilog_tooling.inst import (
    InterfaceDef,
    Keep,
    ModuleDef,
    Port,
    auto_inst,
    auto_inst_update,
    auto_inst_update_order,
    find_autoinst_markers,
    find_interfaces,
    kill_auto_inst,
    main,
    modify_emacs_inst_format,
    parse_interface,
    parse_module_ports,
    resolve_instance,
)
from verilog_tooling.template import find_auto_templates

DATE = "2024-01-01 00:00"

SMALL = """\
module small (
    input  wire       clk,
    input  wire [7:0] din,
    output wire       vld
);
endmodule
"""

# automatic.vim alignment: prefix floor 26, suffix floor 30+12=42,
# margin = max_len - cur_len + 1 spaces.
CLK_LINE = "    .clk" + " " * 24 + "(clk" + " " * 40 + "), // input "
DIN_LINE = "    .din" + " " * 24 + "(din[7:0]" + " " * 35 + "), // input "
VLD_LINE_LAST = "    .vld" + " " * 24 + "(vld" + " " * 40 + ")  // output"


def small_mod() -> ModuleDef:
    return parse_module_ports(SMALL.splitlines())


# ---------------------------------------------------------------------------
# parse_module_ports (s:GetSeqIO)


def test_parse_module_ports_basic():
    moddef = small_mod()
    assert moddef.name == "small"
    assert moddef.ports == [
        Port("clk", "input", None),
        Port("din", "input", "7:0"),
        Port("vld", "output", None),
    ]
    assert moddef.last_port_name() == "vld"


def test_parse_module_ports_keeps_ifdef_comment_and_collapses_blanks():
    lines = """\
module proc (
    input  wire        clk,
`ifdef HAS_CFG
    input  wire [3:0]  cfg,
`endif


    // data path
    output wire [15:0] dout
);
always @(posedge clk) begin
end
endmodule
""".splitlines()
    moddef = parse_module_ports(lines)
    assert moddef.entries == (
        Port("clk", "input", None),
        Keep("`ifdef HAS_CFG"),
        Port("cfg", "input", "3:0", guard=(("HAS_CFG", True),)),
        Keep("`endif"),
        Keep(""),
        Keep("    // data path"),
        Port("dout", "output", "15:0"),
    )


def test_parse_module_ports_ansi_logic_and_parameter_reset():
    lines = """\
module ansi #(
    parameter W = 8

) (
    input  logic          clk,
    , input  logic [W-1:0] din,
    output logic           vld
);
endmodule
""".splitlines()
    moddef = parse_module_ports(lines)
    assert moddef.name == "ansi"
    # keep lines collected inside #( ... ) are reset/trimmed before the ports
    assert not any(isinstance(e, Keep) for e in moddef.entries)
    assert moddef.ports == [
        Port("clk", "input", None, net_type="logic"),
        Port("din", "input", "W-1:0", net_type="logic"),
        Port("vld", "output", None, net_type="logic"),
    ]


def test_parse_module_ports_without_module_is_empty():
    moddef = parse_module_ports(["// nothing here", "wire x;"])
    assert moddef.ports == []


# ---------------------------------------------------------------------------
# resolve_instance (s:GetInstNameAdvance)


def test_resolve_instance_same_line():
    lines = ["proc u_proc_0 (/*autoinst*/);"]
    assert resolve_instance(lines, 0) == ("proc", "u_proc_0")


def test_resolve_instance_with_parameter_override():
    lines = [
        "proc #(",
        "    .W (8)",
        ") u_proc_1 (/*autoinst*/);",
    ]
    assert resolve_instance(lines, 2) == ("proc", "u_proc_1")


def test_resolve_instance_multiline_opener():
    lines = [
        "proc",
        "    u_p (",
        "/*autoinst*/",
    ]
    assert resolve_instance(lines, 2) == ("proc", "u_p")


def test_resolve_instance_rejects_non_instance():
    with pytest.raises(ValueError, match="pair not-match"):
        resolve_instance(["assign x = /*autoinst*/ 1;"], 0)


def test_resolve_instance_mid_port_list_marker():
    """Manual connections may precede the /*autoinst*/ marker: the instance
    resolves through the enclosing paren, so AIO/AW/AR no longer skip it."""
    lines = [
        "proc u_p (",
        "    .sclk_n (sclk_inv),",
        "    /*autoinst*/",
        "    .clk (clk_w),",
        ");",
    ]
    assert resolve_instance(lines, 2) == ("proc", "u_p")


def test_resolve_instance_mid_port_list_marker_with_params():
    lines = [
        "proc #(",
        "    .W (8)",
        ") u_p (",
        "    .sclk_n (sclk_inv),",
        "    /*autoinst*/",
        ");",
    ]
    assert resolve_instance(lines, 4) == ("proc", "u_p")


# ---------------------------------------------------------------------------
# kill_auto_inst + auto_inst (AIT)


def test_kill_auto_inst_collapses_to_stub():
    lines = [
        "small u_s (/*autoinst*/",
        "    .clk (clk), // input",
        "    .vld (vld)  // output",
        ");",
        "assign x = 1;",
    ]
    assert kill_auto_inst(lines) == [
        "small u_s (/*autoinst*/);",
        "assign x = 1;",
    ]


def test_auto_inst_regenerates_with_alignment():
    lines = ["module top;", "small u_small_0 (/*autoinst*/);", "endmodule"]
    out = auto_inst(lines, {"small": small_mod()})
    # NOTE: the regenerated opener keeps the marker comment open; the port
    # list closes at the standalone ');' line (automatic.vim behaviour).
    assert out == [
        "module top;",
        "small u_small_0 (/*autoinst*/",
        CLK_LINE,
        DIN_LINE,
        VLD_LINE_LAST,
        ");",
        "endmodule",
    ]


def test_auto_inst_preserves_keep_lines():
    lines = ["proc u_p (/*autoinst*/);"]
    moddef = parse_module_ports(
        [
            "module proc (",
            "`ifdef HAS_CFG",
            "    input wire [3:0] cfg,",
            "`endif",
            "    // data path",
            "    input wire din,",
            "    output wire vld",
            ");",
            "endmodule",
        ]
    )
    out = auto_inst(lines, {"proc": moddef})
    assert out[0] == "proc u_p (/*autoinst*/"
    assert out[1] == "`ifdef HAS_CFG"
    assert out[2].startswith("    .cfg")
    assert out[3] == "`endif"
    assert out[4] == "    " + "    // data path"  # comment keeps indented
    assert out[-1] == ");"


def test_auto_inst_oneline_mode():
    lines = ["small u_small_1 (/*autoinst --oneline*/);"]
    out = auto_inst(lines, {"small": small_mod()})
    assert out == ["small u_small_1 (/*autoinst --oneline*/ .clk(clk), .din(din[7:0]), .vld(vld));"]


def test_auto_inst_applies_auto_template():
    text = """\
/* small AUTO_TEMPLATE (
    .din (src[@]),
); */
small u_small_3 (/*autoinst*/);
"""
    templates = find_auto_templates(text)
    out = auto_inst(text.splitlines(), {"small": small_mod()}, templates=templates)
    din_line = "    .din" + " " * 24 + "(src[3]" + " " * 37 + "), // input "
    assert out[3] == "small u_small_3 (/*autoinst*/"
    assert out[4] == CLK_LINE  # untemplated ports keep the identity connection
    assert out[5] == din_line
    assert out[6] == VLD_LINE_LAST
    assert out[7] == ");"


def test_auto_inst_which_selects_one_instance():
    lines = [
        "small u_a (/*autoinst*/);",
        "small u_b (/*autoinst*/);",
    ]
    out = auto_inst(lines, {"small": small_mod()}, which=1)
    assert out[0] == "small u_a (/*autoinst*/);"
    assert out[1] == "small u_b (/*autoinst*/"
    assert out[2] == CLK_LINE


def test_auto_inst_missing_module_raises():
    with pytest.raises(KeyError, match="othermod"):
        auto_inst(["othermod u_x (/*autoinst*/);"], {})
    with pytest.raises(ValueError, match="No Instance found"):
        kill_auto_inst(["small u_a (/*autoinst*/);"], which=3)


# ---------------------------------------------------------------------------
# auto_inst_update (AIU1)


def test_auto_inst_update_marks_new_and_deleted():
    lines = [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        "    .gone (gone_sig),",
        ");",
    ]
    out = auto_inst_update(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        f"//    .gone (gone_sig), // INST_DEL: port gone have deleted {DATE}",
        DIN_LINE + f" // INST_NEW {DATE}",
        VLD_LINE_LAST + f" // INST_NEW {DATE}",
        ");",
    ]


def test_auto_inst_update_on_stub_makes_all_ports_new():
    lines = ["small u_s (/*autoinst*/);"]
    out = auto_inst_update(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "small u_s (/*autoinst*/",
        CLK_LINE + f" // INST_NEW {DATE}",
        DIN_LINE + f" // INST_NEW {DATE}",
        VLD_LINE_LAST + f" // INST_NEW {DATE}",
        ");",
    ]


# ---------------------------------------------------------------------------
# auto_inst_update_order (AIU)


def test_auto_inst_update_order_reorders_and_reuses_lines():
    lines = [
        "small u_s (/*autoinst*/",
        "    .vld (vld)",
        "    .clk (my_clk),",
        "    .gone (gone_sig),",
        ");",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",  # reused verbatim, moved to module order
        DIN_LINE + f" // INST_NEW {DATE}",
        "    .vld (vld)",  # old last port stays last: untouched
        f"//    .gone (gone_sig), // INST_DEL: port gone have deleted {DATE}",
        ");",
    ]


def test_auto_inst_update_order_fixes_comma_when_last_port_changes():
    lines = [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        "    .din (din[7:0])   // was last",
        ");",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    # din is no longer the last port: a comma is added; like the Vim original
    # the comment is re-attached directly after the comma-fixed body
    assert out[2] == "    .din (din[7:0]),// was last"
    assert out[3] == VLD_LINE_LAST + f" // INST_NEW {DATE}"
    assert out[4] == ");"


def test_auto_inst_update_order_normalizes_emacs_format_first():
    lines = [
        "module top;",
        "small u_s (/*AUTOINST*/",
        "// Outputs",
        "    .vld (vld),",
        "// Inputs",
        "    .clk (clk),",
        "    .din (din[7:0]));",
        "endmodule",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "module top;",
        "small u_s (/*AUTOINST*/",
        "    .clk (clk),",
        "    .din (din[7:0]),",
        "    .vld (vld)",
        ");",
        "endmodule",
    ]


def test_auto_inst_update_order_delegates_stub_to_auto_inst():
    lines = ["small u_s (/*autoinst*/);"]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "small u_s (/*autoinst*/",
        CLK_LINE,
        DIN_LINE,
        VLD_LINE_LAST,
        ");",
    ]


def test_auto_inst_update_order_keeps_multiline_handwritten_connection():
    # a hand-written connection may span several lines (concat opened on
    # the first line, closed on a later one); the kept text must include
    # every line, or the instance ends mid-expression
    lines = [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        "    .din ({din_hi[3:0],",
        "          din_lo[3:0]}),",
        "    .vld (vld)",
        ");",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    joined = "\n".join(out)
    assert ".din ({din_hi[3:0],\n          din_lo[3:0]})," in joined
    assert "syntax" not in joined  # trivial guard against truncation
    # both continuation words survive exactly once
    assert joined.count("din_hi") == 1 and joined.count("din_lo") == 1


def test_auto_inst_update_order_comments_every_line_of_deleted_multiline():
    # a deleted port whose hand-written connection spans lines: the
    # INST_DEL note is on the first line, but every continuation line
    # must be commented out as well or it becomes live code again
    lines = [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        "    .gone ({gone_hi[3:0],",
        "            gone_lo[3:0]}),",
        "    .vld (vld)",
        ");",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, date=DATE)
    assert out == [
        "small u_s (/*autoinst*/",
        "    .clk (my_clk),",
        DIN_LINE + f" // INST_NEW {DATE}",
        "    .vld (vld)",
        f"//    .gone ({{gone_hi[3:0], // INST_DEL: port gone have deleted {DATE}",
        "//            gone_lo[3:0]}),",
        ");",
    ]


def test_modify_emacs_inst_format():
    out = modify_emacs_inst_format(["    .din (din[7:0])); // data", "plain"])
    assert out == ["    .din (din[7:0])  // data", "); ", "plain"]


def test_auto_inst_update_order_which_list_targets_only_listed():
    # the CLI passes the list of resolvable marker ordinals when some
    # instance modules are missing; the pre-format passes must accept
    # the list (they used to crash with TypeError on '<=' int vs list)
    # and leave unlisted instances byte-identical.
    lines = [
        "module top;",
        "small u_a (/*AUTOINST*/",
        "    .vld (vld),",
        "    .clk (clk),",
        "    .din (din[7:0]));",
        "small u_b (/*AUTOINST*/",
        "    .vld (vld),",
        "    .clk (clk),",
        "    .din (din[7:0]));",
        "endmodule",
    ]
    out = auto_inst_update_order(lines, {"small": small_mod()}, which=[1], date=DATE)
    assert out[:5] == lines[:5]  # u_a verbatim, tail '));' unsplit
    assert out[5:] == [
        "small u_b (/*AUTOINST*/",
        "    .clk (clk),",
        "    .din (din[7:0]),",
        "    .vld (vld)",
        ");",
        "endmodule",
    ]


def test_cli_aiu_skips_instance_with_missing_module(tmp_path, capsys):
    # veerel2 shape: one instance's module (rvoclkhdr) is not in the
    # tree; aiu must warn and process the resolvable instances instead
    # of crashing in the which-list pre-format passes.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text("small u_ok (/*autoinst*/);\nghost u_missing (/*autoinst*/);\n")
    out_file = tmp_path / "out.v"
    main(["aiu", "-i", str(buf), "-o", str(out_file), "-y", str(tmp_path)])
    text = out_file.read_text()
    assert ".clk" in text  # u_ok expanded
    assert "ghost u_missing (/*autoinst*/);" in text  # skipped verbatim
    assert "skipping 1 instance" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI


def test_cli_ait_end_to_end(tmp_path, capsys):
    # `ait` is deprecated: it delegates to the emacs (eai) AUTOINST
    # expansion.  Existing connections are kept (no kill+regenerate) and
    # the deprecation warning goes to stderr; output bytes equal eai's.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text("small u_s (/*autoinst*/);\n")
    out_file = tmp_path / "out.v"
    main(["ait", "-i", str(buf), "-o", str(out_file), "-y", str(tmp_path)])
    eai_file = tmp_path / "eai.v"
    main(["eai", "-i", str(buf), "-o", str(eai_file), "-y", str(tmp_path)])
    assert out_file.read_text() == eai_file.read_text()
    # the emacs expansion reached the full pin list (sanity: not a stub)
    text = out_file.read_text()
    assert ".clk" in text and ".din" in text and ".vld" in text
    assert "deprecated" in capsys.readouterr().err


def test_cli_ait_keeps_hand_renamed_connection(tmp_path, capsys):
    # the motivating case for the deprecation: a hand-renamed connection
    # (.clk(rx_clk)) used to be rewritten to the identity .clk(clk) by
    # ait's kill+regenerate; the eai delegation keeps it verbatim.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text('small u_s (\n    .clk (rx_clk),\n    /*autoinst*/\n);\n')
    out_file = tmp_path / "out.v"
    main(["ait", "-i", str(buf), "-o", str(out_file), "-y", str(tmp_path)])
    text = out_file.read_text()
    assert "rx_clk" in text and ".clk" in text
    assert "deprecated" in capsys.readouterr().err
    # idempotent re-run keeps the rename
    main(["ait", "-i", str(out_file), "-o", str(out_file), "-y", str(tmp_path)])
    assert "rx_clk" in out_file.read_text()


TWO_INST = """\
small u_a (/*autoinst*/
    .clk (clk),
    .din (d0),
    .vld (v0)
);
small u_b (/*autoinst*/
    .clk (clk),
    .din (d1),
    .gone (gone_net),
    .vld (v1)
);
"""


def _line_of(text: str, needle: str) -> int:
    return text.splitlines().index(
        next(ln for ln in text.splitlines() if needle in ln)
    ) + 1


def test_cli_aiu1_line_selects_instance_with_stale_pin(tmp_path):
    # --line is the editor-cursor selector (Vim :AIU without a count):
    # the stale .gone inside u_b is flagged; u_a stays byte-identical.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text(TWO_INST)
    out_file = tmp_path / "out.v"
    main(
        [
            "aiu1",
            "-i",
            str(buf),
            "-o",
            str(out_file),
            "-y",
            str(tmp_path),
            "--date",
            "[D]",
            "--line",
            str(_line_of(TWO_INST, ".gone")),
        ]
    )
    out = out_file.read_text().splitlines()
    a_part, b_part = out[:6], out[6:]
    assert a_part == TWO_INST.splitlines()[:6]  # u_a untouched
    assert b_part == [
        "    .clk (clk),",
        "    .din (d1),",
        "//    .gone (gone_net), // INST_DEL: port gone have deleted [D]",
        "    .vld (v1)",
        ");",
    ]


def test_cli_aiu1_line_on_header_and_closer_selects_same_instance(tmp_path):
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text(TWO_INST)
    # 11 = the ');' line closing u_b (TWO_INST has 11 lines)
    for line_no in (_line_of(TWO_INST, "small u_b"), 11):
        out_file = tmp_path / "out.v"
        main(
            [
                "aiu1",
                "-i",
                str(buf),
                "-o",
                str(out_file),
                "-y",
                str(tmp_path),
                "--date",
                "[D]",
                "--line",
                str(line_no),
            ]
        )
        assert "INST_DEL" in out_file.read_text()


def test_cli_aiu1_line_outside_instance_fails(tmp_path):
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text("// top note\n" + TWO_INST)  # line 1 is outside any instance
    with pytest.raises(SystemExit, match="contains line 1"):
        main(
            [
                "aiu1",
                "-i",
                str(buf),
                "-o",
                str(tmp_path / "out.v"),
                "-y",
                str(tmp_path),
                "--line",
                "1",
            ]
        )


def test_cli_aiu1_line_in_markerless_instance_fails(tmp_path):
    buf = tmp_path / "top.v"
    plain = "small u_p (\n    .clk (clk)\n);\n"
    buf.write_text(plain)
    with pytest.raises(SystemExit, match="contains line 2"):
        main(
            [
                "aiu1",
                "-i",
                str(buf),
                "-o",
                str(tmp_path / "out.v"),
                "-y",
                str(tmp_path),
                "--line",
                "2",
            ]
        )


def test_cli_line_conflicts_with_which(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text(TWO_INST)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        main(
            [
                "aiu1",
                "-i",
                str(buf),
                "-o",
                str(tmp_path / "out.v"),
                "--line",
                "3",
                "--which",
                "0",
            ]
        )


def test_cli_line_rejected_for_all_run_verbs(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text(TWO_INST)
    with pytest.raises(SystemExit, match="--line applies only to"):
        main(
            [
                "af",
                "-i",
                str(buf),
                "-o",
                str(tmp_path / "out.v"),
                "--line",
                "3",
            ]
        )


def test_cli_line_rejected_for_non_aiu_family(tmp_path):
    # --line is AIU-family only (ait/aiu/aiu1); every other command
    # stays whole-file even though it accepts --which.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text(
        "small u_a (/*autoinst*/);\n"
        "small u_b (/*autoinst*/);\n"
    )
    for cmd in ("eai", "eap", "kill"):
        with pytest.raises(SystemExit, match="--line applies only to"):
            main(
                [
                    cmd,
                    "-i",
                    str(buf),
                    "-o",
                    str(tmp_path / "out.v"),
                    "-y",
                    str(tmp_path),
                    "--line",
                    "2",
                ]
            )


def test_cli_ait_line_expands_only_that_instance(tmp_path):
    # ait delegates to eai but keeps the AIU-family --line behaviour.
    (tmp_path / "small.v").write_text(SMALL)
    buf = tmp_path / "top.v"
    buf.write_text(
        "small u_a (/*autoinst*/);\n"
        "small u_b (/*autoinst*/);\n"
    )
    out_file = tmp_path / "out.v"
    main(
        [
            "ait",
            "-i",
            str(buf),
            "-o",
            str(out_file),
            "-y",
            str(tmp_path),
            "--line",
            "2",
        ]
    )
    out = out_file.read_text().splitlines()
    assert out[0] == "small u_a (/*autoinst*/);"  # untouched
    joined = "\n".join(out[1:])
    assert ".clk" in joined and ".vld" in joined


def test_cli_missing_module_file_fails(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("nope u_s (/*autoinst*/);\n")
    with pytest.raises(SystemExit, match="nope"):
        main(["ait", "-i", str(buf), "-o", str(tmp_path / "out.v"), "-y", str(tmp_path)])


# ---------------------------------------------------------------------------
# leading-comma (ANSI `, input ...`) port declarations — automatic.vim
# s:GetSeqIO / GetIO both accept a leading `,` on a port-declaration line.


def test_parse_module_ports_leading_comma():
    lines = [
        "module m (",
        "      input wire        clk",
        "    , input wire [7:0]  din",
        "    , output reg        vld",
        "    , output wire [3:0] stat",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("clk", "input", None),
        ("din", "input", "7:0"),
        ("vld", "output", None),
        ("stat", "output", "3:0"),
    ]
    # AIT regenerates all four ports from such a module definition
    out = auto_inst(["module top;", "m u_m (/*autoinst*/);", "endmodule"], {"m": moddef})
    body = "\n".join(out)
    for name in ("clk", "din", "vld", "stat"):
        assert f".{name}" in body


# ---------------------------------------------------------------------------
# real-world module declaration forms (found in the tx5515 RTL tree)


def test_parse_port_wire_no_space_and_signed():
    lines = [
        "module m (",
        "    input  wire[31:0]   a,",   # `wire[` with no space
        "    output reg  [7:0]    b,",
        "    output signed [15:0] c,",  # signed
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("a", "input", "31:0"),
        ("b", "output", "7:0"),
        ("c", "output", "15:0"),
    ]


# ---------------------------------------------------------------------------
# legal-Verilog formatting robustness (packed-range spacing, multi-line
# declarations, inline comments) — A1-A4 / C10


def test_parse_module_ports_range_whitespace_normalised():
    lines = [
        "module m (",
        "    input  wire [ 7 : 0 ] din,",   # spaces inside the packed range
        "    output reg  [ 15 : 0] dout",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("din", "input", "7:0"),
        ("dout", "output", "15:0"),
    ]


def test_parse_module_ports_multiline_decl_name_on_next_line():
    lines = [
        "module m (",
        "    input  wire [7:0]",
        "        din,",
        "    output reg  [3:0]",
        "        stat",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("din", "input", "7:0"),
        ("stat", "output", "3:0"),
    ]


def test_parse_module_ports_no_space_before_range():
    lines = [
        "module m (",
        "    input[3:0]a,",
        "    output wire[1:0]b",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("a", "input", "3:0"),
        ("b", "output", "1:0"),
    ]


def test_parse_module_ports_inline_block_comment_between_keyword_and_range():
    lines = [
        "module m (",
        "    input  /* opt */ wire [3:0] x,",   # comment inside the declaration
        "    output wire [3:0] y, /* trailing */ // line",
        "    input  [7:0] z,   /* trailing comment on same line */",
        ");",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("x", "input", "3:0"),
        ("y", "output", "3:0"),
        ("z", "input", "7:0"),
    ]


def test_parse_module_ports_multiline_join_never_eats_statement():
    # an incomplete header port (nameless 'input [7:0]') must NOT join the
    # following statement when no name comes: the join stops at ');' and the
    # always block stays untouched
    lines = [
        "module m (",
        "    input [7:0]",
        ");",
        "always @(*) begin",
        "end",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert moddef.port_names() == []  # nameless line skipped, no exception


def test_parse_module_ports_ansi_single_line_header():
    lines = ["module t(input clk, input [3:0] d, output [7:0] q);", "endmodule"]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("clk", "input", None),
        ("d", "input", "3:0"),
        ("q", "output", "7:0"),
    ]


def test_parse_module_ports_ansi_header_with_params():
    lines = [
        "module t #(",
        "    parameter W = 8",
        ") (input clk, output reg [W-1:0] d);",
        "endmodule",
    ]
    moddef = parse_module_ports(lines)
    assert [(p.name, p.direction, p.width) for p in moddef.ports] == [
        ("clk", "input", None),
        ("d", "output", "W-1:0"),
    ]


# ---------------------------------------------------------------------------
# SystemVerilog interface ports

IFACE = """\
interface cpu_bus;
    logic        req;
    logic [31:0] addr;
    logic        gnt;
    modport master (output req, addr, input gnt);
    modport slave  (input req, addr, output gnt);
endinterface
"""

SUB_IF = """\
module sub (
    cpu_bus.master bus,
    cpu_bus        misc,
    input  wire       clk,
    input  wire [7:0] din,
    output wire       done
);
endmodule
"""

SUB_IF_ANSI = """\
module sub (cpu_bus.master bus, cpu_bus misc, input clk, input [7:0] din, output done);
endmodule
"""


def sub_if_mod() -> ModuleDef:
    return parse_module_ports(SUB_IF.splitlines(), interfaces={"cpu_bus"})


def test_parse_interface():
    idef = parse_interface(IFACE.splitlines())
    assert idef == InterfaceDef("cpu_bus", ("master", "slave"))


def test_parse_interface_missing_raises():
    with pytest.raises(ValueError, match="no interface"):
        parse_interface(["module m; endmodule"])


def test_find_interfaces(tmp_path):
    (tmp_path / "cpu_bus.sv").write_text(IFACE)
    (tmp_path / "plain.v").write_text("module plain; endmodule\n")
    found = find_interfaces([str(tmp_path)])
    assert found == {"cpu_bus": tmp_path / "cpu_bus.sv"}


def test_parse_module_ports_interface_ports():
    moddef = sub_if_mod()
    assert moddef.ports == [
        Port("bus", "interface", is_interface=True, modport="master", iface="cpu_bus"),
        Port("misc", "interface", is_interface=True, iface="cpu_bus"),
        Port("clk", "input", None),
        Port("din", "input", "7:0"),
        Port("done", "output", None),
    ]


def test_parse_module_ports_interface_ansi_header():
    moddef = parse_module_ports(SUB_IF_ANSI.splitlines(), interfaces={"cpu_bus"})
    assert [(p.name, p.direction, p.modport) for p in moddef.ports] == [
        ("bus", "interface", "master"),
        ("misc", "interface", None),
        ("clk", "input", None),
        ("din", "input", None),
        ("done", "output", None),
    ]


def test_parse_module_ports_unknown_interface_names_ignored():
    # without the interface-name set the lines are not port declarations
    moddef = parse_module_ports(SUB_IF.splitlines())
    assert moddef.port_names() == ["clk", "din", "done"]


def test_auto_inst_regenerates_interface_ports():
    buf = ["sub u_sub (/*autoinst*/);"]
    out = auto_inst(buf, {"sub": sub_if_mod()})
    # automatic.vim alignment floors: prefix 26, suffix 42; interface ports
    # connect as name.modport (or plain name) with a '// interface' comment
    assert out == [
        "sub u_sub (/*autoinst*/",
        "    .bus" + " " * 24 + "(bus.master" + " " * 33 + "), // interface",
        "    .misc" + " " * 23 + "(misc" + " " * 39 + "), // interface",
        "    .clk" + " " * 24 + "(clk" + " " * 40 + "), // input ",
        "    .din" + " " * 24 + "(din[7:0]" + " " * 35 + "), // input ",
        "    .done" + " " * 23 + "(done" + " " * 39 + ")  // output",
        ");",
    ]


def test_auto_inst_update_appends_new_interface_port():
    buf = [
        "sub u_sub (/*autoinst*/",
        "    .clk (clk),",
        "    .din (din[7:0]),",
        "    .done (done)",
        ");",
    ]
    out = auto_inst_update(buf, {"sub": sub_if_mod()}, date=DATE)
    new = [ln for ln in out if "INST_NEW" in ln]
    assert "(bus.master" in new[0] and "// interface" in new[0]
    assert "(misc" in new[1]
    # pre-existing lines are kept verbatim
    assert "    .clk (clk)," in out


def test_auto_inst_update_preserves_handwritten_interface_line():
    buf = [
        "sub u_sub (/*autoinst*/",
        "    .bus  (hand_bus.master),",
        "    .misc (misc),",
        "    .clk  (clk),",
        "    .din  (din[7:0]),",
        "    .done (done)",
        ");",
    ]
    out = auto_inst_update(buf, {"sub": sub_if_mod()}, date=DATE)
    assert "    .bus  (hand_bus.master)," in out
    assert not any("INST_NEW" in ln or "INST_DEL" in ln for ln in out)


def test_auto_inst_update_order_reuses_handwritten_interface_line():
    buf = [
        "sub u_sub (/*autoinst*/",
        "    .done (done),",
        "    .bus (hand_bus.master),",
        "    .clk (clk),",
        "    .misc (misc),",
        "    .din (din[7:0])",
        ");",
    ]
    out = auto_inst_update_order(buf, {"sub": sub_if_mod()}, date=DATE)
    # module port order, hand-written interface connection reused verbatim
    assert out == [
        "sub u_sub (/*autoinst*/",
        "    .bus (hand_bus.master),",
        "    .misc (misc),",
        "    .clk (clk),",
        "    .din (din[7:0]),",
        "    .done (done)",
        ");",
    ]


# ---------------------------------------------------------------------------
# AIT/AIU support the full EAI (verilog-mode) AUTO_TEMPLATE regexp features


def _mk_mod():
    return parse_module_ports(
        """module m (
    input clk,
    input [7:0] pci_req0_l,
    input [7:0] pci_req2_l,
    input [7:0] pci_req10_l,
    output done
);
endmodule""".splitlines()
    )


def test_ait_regexp_port_pattern_backref_and_at():
    from verilog_tooling.template import find_auto_templates
    text = '''/* m AUTO_TEMPLATE (
    .pci_req\\([0-9]+\\)_l (pci_req_jtag_@[\\1]),
); */
m u_m_3 (/*autoinst*/);
'''
    out = auto_inst(text.splitlines(), {"m": _mk_mod()},
                    templates=find_auto_templates(text))
    body = "\n".join(out)
    assert "pci_req_jtag_3[0]" in body
    assert "pci_req_jtag_3[2]" in body
    assert "pci_req_jtag_3[10]" in body


def test_aiu_regexp_port_pattern_backref():
    from verilog_tooling.template import find_auto_templates
    text = '''/* m AUTO_TEMPLATE (
    .pci_req\\([0-9]+\\)_l (pci_req_jtag_@[\\1]),
); */
m u_m_3 (/*autoinst*/);
'''
    out = auto_inst_update_order(text.splitlines(), {"m": _mk_mod()},
                                 templates=find_auto_templates(text))
    body = "\n".join(out)
    assert "pci_req_jtag_3[0]" in body
    assert "pci_req_jtag_3[2]" in body


def test_ait_at_in_port_pattern_and_custom_regexp():
    from verilog_tooling.template import find_auto_templates
    mod = parse_module_ports(
        """module m (
    input [3:0] in0,
    input [3:0] in1,
    input [3:0] inx,
    output done
);
endmodule""".splitlines()
    )
    # @ in port pattern -> digit group, \1 refers to it
    text = '''/* m AUTO_TEMPLATE (
    .in@ (out[\\1]),
); */
m u_m_0 (/*autoinst*/);
'''
    out = auto_inst(text.splitlines(), {"m": mod}, templates=find_auto_templates(text))
    body = "\n".join(out)
    assert "(out[0]" in body and "(out[1]" in body and "inx[3:0]" in body
    # custom @ regexp
    text2 = r'''/* m AUTO_TEMPLATE "_\([a-z]+\)" (
    .in0 (sig_@),
); */
m u_m_FOO (/*autoinst*/);
'''
    out2 = auto_inst(text2.splitlines(), {"m": mod}, templates=find_auto_templates(text2))
    assert "sig_m" in "\n".join(out2)


def test_ait_empty_brackets_port_range():
    from verilog_tooling.template import find_auto_templates
    mod = parse_module_ports(
        """module m (
    input [3:0] in0,
    output done
);
endmodule""".splitlines()
    )
    text = '''/* m AUTO_TEMPLATE (
    .in0 (net[]),
); */
m u_m_0 (/*autoinst*/);
'''
    out = auto_inst(text.splitlines(), {"m": mod}, templates=find_auto_templates(text))
    assert "net[3:0]" in "\n".join(out)


def test_aiu_template_wins_over_manual_connection():
    """AIU: a port the AUTO_TEMPLATE declares follows the template (marked
    // Templated); ports it does not declare keep their manual connection."""
    text = """\
/* small AUTO_TEMPLATE (
    .din (custom_bus[]),
); */
small u_s (/*autoinst*/
    .clk (my_clk),
    .din (din[7:0]),
    .vld (vld_w)
);
"""
    templates = find_auto_templates(text)
    out = auto_inst_update_order(
        text.splitlines(), {"small": small_mod()}, templates=templates, date=DATE
    )
    body = "\n".join(out)
    assert "custom_bus[7:0]" in body and "// Templated" in body  # template wins
    assert ".clk (my_clk)," in body  # untemplated: manual kept verbatim
    assert ".vld (vld_w)" in body
    out2 = auto_inst_update_order(out, {"small": small_mod()}, templates=templates, date=DATE)
    assert out2 == out  # idempotent


def test_aiu_minimal_diff_template_wins():
    """auto_inst_update (minimal-diff): same template-wins rule."""
    text = """\
/* small AUTO_TEMPLATE (
    .din (custom_bus[]),
); */
small u_s (/*autoinst*/
    .clk (my_clk),
    .din (din[7:0]),
    .vld (vld_w)
);
"""
    templates = find_auto_templates(text)
    out = auto_inst_update(
        text.splitlines(), {"small": small_mod()}, templates=templates, date=DATE
    )
    body = "\n".join(out)
    assert "custom_bus[7:0]" in body and "// Templated" in body
    assert ".clk (my_clk)," in body
    assert ".vld (vld_w)" in body


def test_parse_module_ports_inout_net_types():
    """Explicit net types on inout ports (pad cells often use `inout tri`):
    the type keyword must not be mistaken for the port name."""
    src = """\
module pad (
    input  wire       clk,
    inout  tri  [7:0] gpio,
    inout  tri1         pull_up,
    inout  trireg [3:0] hold,
    inout  wor          w_or
);
endmodule
"""
    ports = {p.name: (p.direction, p.width) for p in parse_module_ports(src.splitlines()).ports}
    assert ports == {
        "clk": ("input", None),
        "gpio": ("inout", "7:0"),
        "pull_up": ("inout", None),
        "hold": ("inout", "3:0"),
        "w_or": ("inout", None),
    }


def test_cli_aiu1_line_does_not_reformat_other_instances(tmp_path):
    """--line targets one instance: another instance with an
    emacs-style tail (.q(qb)); must stay BYTE-IDENTICAL — the head-move
    and tail-split normalisations are gated to the target."""
    sub = tmp_path / "sub.v"
    sub.write_text(
        "module sub (input clk, input rst_n, input [3:0] d, output [3:0] q);\nendmodule\n"
    )
    top = tmp_path / "top.v"
    src = (
        "module top;\n"
        "  sub u_a (/*autoinst*/\n"
        "           .clk (clk),\n"
        "           .d   (da),\n"
        "           .q   (qa));\n"
        "  sub u_b (/*autoinst*/\n"
        "           .clk (clk),\n"
        "           .d   (db),\n"
        "           .q   (qb));\n"
        "endmodule\n"
    )
    top.write_text(src)
    out_file = tmp_path / "out.v"
    main(["aiu1", "-i", str(top), "-o", str(out_file), "-y", str(tmp_path), "--line", "9"])
    out = out_file.read_text()
    # u_a (lines 1-6) byte-identical; u_b gained the rst_n pin
    assert out.splitlines()[:5] == src.splitlines()[:5]
    assert "rst_n" in out


# ---------------------------------------------------------------------------
# preprocessor-guarded ports (`ifdef families)

_GSUB_LINES = [
    "module gsub (",
    "    input  wire       clk,",
    "`ifdef HAS_EXTRA",
    "    input  wire       extra,",
    "`endif",
    "    input  wire [7:0] din,",
    "    output wire [7:0] dout",
    ");",
    "endmodule",
]


def _gsub_mod():
    return parse_module_ports(_GSUB_LINES)


def test_parse_ports_records_guard_conditions():
    moddef = _gsub_mod()
    guards = {p.name: p.guard for p in moddef.ports}
    assert guards["clk"] == ()
    assert guards["extra"] == (("HAS_EXTRA", True),)
    assert guards["din"] == ()
    sub = parse_module_ports(
        [
            "module g2 (",
            "`ifdef MODE_A",
            "    output wire oa,",
            "`elsif MODE_B",
            "    output wire ob,",
            "`else",
            "    output wire oc,",
            "`endif",
            "    output wire dout",
            ");",
            "endmodule",
        ]
    )
    g2 = {p.name: p.guard for p in sub.ports}
    assert g2["oa"] == (("MODE_A", True),)
    assert g2["ob"] == (("MODE_A", False), ("MODE_B", True))
    assert g2["oc"] == (("MODE_A", False), ("MODE_B", False))
    assert g2["dout"] == ()


def test_aiu_new_guarded_pin_is_wrapped():
    lines = [
        "module top;",
        "gsub u_g (/*autoinst*/",
        "    .clk (clk)",
        ");",
        "endmodule",
    ]
    out = auto_inst_update(lines, {"gsub": _gsub_mod()}, date=DATE)
    joined = "\n".join(out)
    gi = joined.index("`ifdef HAS_EXTRA")
    ei = joined.index("`endif")
    xi = joined.index(".extra")
    assert gi < xi < ei
    # kept pin keeps its comma (a later unguarded pin always follows)
    assert "    .clk (clk)," in out


def test_aiu_stub_wraps_guarded_ports():
    lines = ["gsub u_g (/*autoinst*/);"]
    out = auto_inst_update(lines, {"gsub": _gsub_mod()}, date=DATE)
    joined = "\n".join(out)
    assert "`ifdef HAS_EXTRA" in joined
    assert "`endif" in joined
    assert joined.index("`ifdef HAS_EXTRA") < joined.index(".extra")
    # the last pin (dout) is unguarded: no separator comma on it
    (dout_line,) = [ln for ln in out if ".dout" in ln]
    assert "), //" not in dout_line.split("// INST_NEW")[0].rstrip(",")[-12:]


def test_aiu_keeps_user_written_guard_lines():
    lines = [
        "module top;",
        "gsub u_g (/*autoinst*/",
        "    .clk (clk),",
        "`ifdef HAS_EXTRA",
        "    .extra (extra),",
        "`endif",
        "    .din (din)",
        ");",
        "endmodule",
    ]
    out = auto_inst_update(lines, {"gsub": _gsub_mod()}, date=DATE)
    # user directives ride through verbatim, pins are not duplicated
    assert "`ifdef HAS_EXTRA" in out
    assert "`endif" in out
    assert sum(".extra" in ln for ln in out) == 1
    assert sum(".dout" in ln for ln in out) == 1  # only the new pin
    assert "    .din (din)," in out  # comma added for the appended pin


def test_auto_inst_regen_guarded_last_port():
    sub = parse_module_ports(
        [
            "module g3 (",
            "    input wire clk,",
            "    input wire din,",
            "`ifdef TAIL_G",
            "    output wire tail",
            "`endif",
            ");",
            "endmodule",
        ]
    )
    out = auto_inst(["g3 u_g (/*autoinst*/);"], {"g3": sub})
    stripped = [ln.strip() for ln in out]
    (din_line,) = [ln for ln in stripped if ln.startswith(".din")]
    (tail_line,) = [ln for ln in stripped if "tail" in ln and ln.startswith(",")]
    # previous pin lost its trailing comma; the separator moved inside
    # the guard as a leading comma on the guarded pin
    assert not din_line.split("//")[0].rstrip().endswith(",")
    assert stripped[-2] == "`endif"
    assert stripped[-1] == ");"
