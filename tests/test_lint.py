"""verilog-auto-template-lint (ATLINT): warn about unused AUTO_TEMPLATE lines."""

from verilog_tooling import inst, lint

SUB = """\
module sub (
    input  wire       clk,
    input  wire [7:0] din,
    output wire [7:0] dout,
    output wire       done
);
endmodule
"""


def _mods():
    return {"sub": inst.parse_module_ports(SUB.splitlines(), with_params=True)}


def test_lint_warns_about_unused_template_entry():
    lines = [
        "module top;",
        "    /* sub AUTO_TEMPLATE (",
        "        .din (data_in),",
        "        .nosuch (nosuch),",
        "    ) */",
        "    sub u_sub (/*autoinst*/);",
        "endmodule",
    ]
    warnings = lint.lint_templates(lines, _mods(), "top.v")
    assert len(warnings) == 1
    assert "nosuch" in warnings[0]
    assert warnings[0].startswith("top.v:")


def test_lint_silent_when_all_entries_used():
    lines = [
        "module top;",
        "    /* sub AUTO_TEMPLATE (",
        "        .din (data_in),",
        "    ) */",
        "    sub u_sub (/*autoinst*/);",
        "endmodule",
    ]
    assert lint.lint_templates(lines, _mods(), "top.v") == []


def test_lint_ignores_template_for_module_not_instantiated():
    lines = [
        "module top;",
        "    /* sub AUTO_TEMPLATE (",
        "        .nosuch (nosuch),",
        "    ) */",
        "endmodule",
    ]
    assert lint.lint_templates(lines, _mods(), "top.v") == []
