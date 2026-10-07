"""automatic.vim AutoModule/AutoModuleEmacs/AutoPara/AutoFsm rewritten in
Python (gen.py).  All expected outputs were verified against real Vim 9.1
running the original automatic.vim functions."""

import pytest

from verilog_tooling.gen import (
    VerilogBuffer,
    auto_fsm,
    auto_module,
    auto_module_emacs,
    auto_para,
    kill_auto_fsm,
    kill_auto_para,
    main,
)

M = "    "  # s:indent (4 spaces)


# ---------------------------------------------------------------------------
# auto_module (AM)


def test_auto_module_bare_module_name():
    assert auto_module(["fifo"], 0) == ["fifo  u0_fifo(/*autoinst*/);"]


def test_auto_module_keeps_params_with_whitespace_stripped():
    assert auto_module(["fifo #(W=8, D=16)"], 0) == [
        "fifo#(W=8,D=16)  u1_fifo(/*autoinst*/);"
    ]


def test_auto_module_counts_prior_instances():
    lines = ["fifo  u0_fifo(/*autoinst*/);", "fifo"]
    assert auto_module(lines, 1) == [
        "fifo  u0_fifo(/*autoinst*/);",
        "fifo  u1_fifo(/*autoinst*/);",
    ]


def test_auto_module_counts_only_matching_module_names():
    lines = ["ram  u0_ram(/*autoinst*/);", "fifo"]
    assert auto_module(lines, 1) == [
        "ram  u0_ram(/*autoinst*/);",
        "fifo  u0_fifo(/*autoinst*/);",
    ]


def test_auto_module_strips_leading_indent_of_cursor_line():
    lines = ["module top;", "  fifo", "endmodule"]
    assert auto_module(lines, 1) == [
        "module top;",
        "fifo  u0_fifo(/*autoinst*/);",
        "endmodule",
    ]


# ---------------------------------------------------------------------------
# auto_module_emacs (AME)


def test_auto_module_emacs_adds_template_block_and_autoinstparam():
    assert auto_module_emacs(["fifo"], 0) == [
        "/* fifo  auto_template ( ",
        "  ); */",
        "fifo #(/*autoinstparam*/)   u0_fifo(/*autoinst*/);",
    ]


def test_auto_module_emacs_keeps_given_params():
    assert auto_module_emacs(["fifo #(W=8)"], 0) == [
        "/* fifo  auto_template ( ",
        "  ); */",
        "fifo#(W=8)  u1_fifo(/*autoinst*/);",
    ]


def test_auto_module_emacs_counts_prior_instances():
    lines = ["fifo #(/*autoinstparam*/)   u0_fifo(/*autoinst*/);", "fifo"]
    assert auto_module_emacs(lines, 1) == [
        "fifo #(/*autoinstparam*/)   u0_fifo(/*autoinst*/);",
        "/* fifo  auto_template ( ",
        "  ); */",
        "fifo #(/*autoinstparam*/)   u1_fifo(/*autoinst*/);",
    ]


# ---------------------------------------------------------------------------
# auto_para (APM)


def test_auto_para_default_value_threading():
    lines = ["module t;", "/*autopara*/ (A, B=2, C)", "endmodule"]
    assert auto_para(lines) == [
        "module t;",
        "/*autopara*/ (A, B=2, C)",
        "// Define parameter here",
        "parameter A = 2'd0;",
        "parameter B = 2'd2;",
        "parameter C = 2'd3;",
        "// End of automatic parameter",
        "endmodule",
    ]


def test_auto_para_width_calc_and_margin():
    # values up to 8 -> 4 bits; RUN (len 3) is one short of maxlen 4, so the
    # >= CalMargin pads it to 5 columns; names at maxlen get a single space.
    lines = ["module t;", "/*autopara*/ (IDLE, RUN, DONE=8, WAIT)", "endmodule"]
    assert auto_para(lines) == [
        "module t;",
        "/*autopara*/ (IDLE, RUN, DONE=8, WAIT)",
        "// Define parameter here",
        "parameter IDLE = 4'd0;",
        "parameter RUN  = 4'd1;",
        "parameter DONE = 4'd8;",
        "parameter WAIT = 4'd9;",
        "// End of automatic parameter",
        "endmodule",
    ]


def test_auto_para_single_parameter_width_one():
    lines = ["module t;", "/*autopara*/ (ON)", "endmodule"]
    assert auto_para(lines) == [
        "module t;",
        "/*autopara*/ (ON)",
        "// Define parameter here",
        "parameter ON = 1'd0;",
        "// End of automatic parameter",
        "endmodule",
    ]


def test_kill_auto_para_round_trip():
    src = ["module t;", "/*autopara*/ (A, B=2, C)", "endmodule"]
    expanded = auto_para(src)
    assert kill_auto_para(expanded) == src
    # lines outside the region pass through
    assert kill_auto_para(["wire a;", "endmodule"]) == ["wire a;", "endmodule"]


def test_auto_para_is_idempotent():
    src = ["module t;", "/*autopara*/ (A, B=2, C)", "endmodule"]
    once = auto_para(src)
    assert auto_para(once) == once


# ---------------------------------------------------------------------------
# auto_fsm (AFM)


def test_auto_fsm_two_block_skeleton():
    lines = ["module t;", "/*autofsm*/ (IDLE,RUN,DONE) state nstate", "endmodule"]
    assert auto_fsm(lines) == [
        "module t;",
        "/*autofsm*/ (IDLE,RUN,DONE) state nstate",
        "// Define fsm here",
        "// Define FSM parameter here",
        "localparam IDLE = 2'd0;",
        "localparam RUN  = 2'd1;",
        "localparam DONE = 2'd2;",
        "// End of automatic parameter for FSM",
        "always @(posedge clk or negedge rst_n) begin",
        M + "if(!rst_n) begin",
        M * 2 + "state[1:0] <= #`RD IDLE;",
        M + "end else begin",
        M * 2 + "state[1:0] <= #`RD nstate[1:0];",
        M + "end",
        "end",
        "always @(*) begin",
        M + "nstate[1:0] = state[1:0];",
        M + "case(state[1:0])",
        M * 2 + "IDLE: begin",
        M * 2 + "end",
        M * 2 + "RUN: begin",
        M * 2 + "end",
        M * 2 + "DONE: begin",
        M * 2 + "end",
        M * 2 + "default: begin",
        M * 2 + "end",
        M + "endcase",
        "end",
        "// End of automatic fsm",
        "endmodule",
    ]


def test_auto_fsm_default_next_state_keeps_vim_trailing_space():
    out = auto_fsm(["module t;", "/*autofsm*/ (IDLE,RUN,DONE) state", "endmodule"])
    assert M * 2 + "state[1:0] <= #`RD next_state [1:0];" in out
    assert M + "next_state [1:0] = state[1:0];" in out


def test_auto_fsm_explicit_values_drive_state_width():
    out = auto_fsm(
        ["module t;", "/*autofsm*/ (IDLE,RUN,DONE=8,WAIT) state nstate", "endmodule"]
    )
    assert "localparam DONE = 4'd8;" in out
    assert "localparam WAIT = 4'd9;" in out
    assert M * 2 + "state[3:0] <= #`RD IDLE;" in out
    assert M + "case(state[3:0])" in out


def test_auto_fsm_strips_existing_signal_width():
    out = auto_fsm(
        ["module t;", "/*autofsm*/ (IDLE,RUN,DONE) state[7:0] nstate", "endmodule"]
    )
    assert M * 2 + "state[1:0] <= #`RD IDLE;" in out
    assert M + "nstate[1:0] = state[1:0];" in out


def test_kill_auto_fsm_round_trip():
    src = ["module t;", "/*autofsm*/ (IDLE,RUN,DONE) state nstate", "endmodule"]
    expanded = auto_fsm(src)
    assert kill_auto_fsm(expanded) == src
    assert kill_auto_fsm(["wire a;", "endmodule"]) == ["wire a;", "endmodule"]


def test_auto_fsm_is_idempotent():
    src = ["module t;", "/*autofsm*/ (IDLE,RUN,DONE) state nstate", "endmodule"]
    once = auto_fsm(src)
    assert auto_fsm(once) == once


# ---------------------------------------------------------------------------
# VerilogBuffer methods


def test_buffer_methods_return_new_buffers():
    buf = VerilogBuffer(["fifo"])
    stubbed = buf.auto_module(0)
    assert isinstance(stubbed, VerilogBuffer)
    assert stubbed.lines == ["fifo  u0_fifo(/*autoinst*/);"]
    assert buf.lines == ["fifo"]  # untouched

    emacsed = buf.auto_module_emacs(0)
    assert isinstance(emacsed, VerilogBuffer)
    assert emacsed.lines[0] == "/* fifo  auto_template ( "

    src = ["module t;", "/*autopara*/ (A, B=2, C)", "endmodule"]
    para_buf = VerilogBuffer(src)
    assert para_buf.auto_para().lines == auto_para(src)
    assert para_buf.kill_auto_para().lines == src

    fsm_src = ["module t;", "/*autofsm*/ (IDLE,RUN) state nstate", "endmodule"]
    fsm_buf = VerilogBuffer(fsm_src)
    assert fsm_buf.auto_fsm().lines == auto_fsm(fsm_src)
    assert fsm_buf.kill_auto_fsm().lines == fsm_src


# ---------------------------------------------------------------------------
# CLI


def test_cli_am_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("fifo\n")
    out_file = tmp_path / "out.v"
    main(["am", "-i", str(buf), "-o", str(out_file), "--line", "1"])
    assert out_file.read_text().splitlines() == ["fifo  u0_fifo(/*autoinst*/);"]


def test_cli_am_line_zero_picks_first_word_bearing_line(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("// header\nfifo\n")
    out_file = tmp_path / "out.v"
    main(["am", "-i", str(buf), "-o", str(out_file), "--line", "0"])
    assert out_file.read_text().splitlines() == [
        "// header",
        "fifo  u0_fifo(/*autoinst*/);",
    ]


def test_cli_ame_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("fifo #(W=8)\n")
    out_file = tmp_path / "out.v"
    main(["ame", "-i", str(buf), "-o", str(out_file), "--line", "1"])
    assert out_file.read_text().splitlines() == [
        "/* fifo  auto_template ( ",
        "  ); */",
        "fifo#(W=8)  u1_fifo(/*autoinst*/);",
    ]


def test_cli_am_requires_line(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("fifo\n")
    with pytest.raises(SystemExit):
        main(["am", "-i", str(buf), "-o", str(tmp_path / "out.v")])


def test_cli_apm_and_kill_para_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("module t;\n/*autopara*/ (A, B=2, C)\nendmodule\n")
    out_file = tmp_path / "out.v"
    main(["apm", "-i", str(buf), "-o", str(out_file)])
    expanded = out_file.read_text().splitlines()
    assert "parameter B = 2'd2;" in expanded
    back = tmp_path / "back.v"
    main(["kill-para", "-i", str(out_file), "-o", str(back)])
    assert back.read_text().splitlines() == [
        "module t;",
        "/*autopara*/ (A, B=2, C)",
        "endmodule",
    ]


def test_cli_afm_and_kill_fsm_end_to_end(tmp_path):
    buf = tmp_path / "top.v"
    buf.write_text("module t;\n/*autofsm*/ (IDLE,RUN,DONE) state nstate\nendmodule\n")
    out_file = tmp_path / "out.v"
    main(["afm", "-i", str(buf), "-o", str(out_file)])
    expanded = out_file.read_text().splitlines()
    assert "localparam DONE = 2'd2;" in expanded
    assert "always @(posedge clk or negedge rst_n) begin" in expanded
    back = tmp_path / "back.v"
    main(["kill-fsm", "-i", str(out_file), "-o", str(back)])
    assert back.read_text().splitlines() == [
        "module t;",
        "/*autofsm*/ (IDLE,RUN,DONE) state nstate",
        "endmodule",
    ]
