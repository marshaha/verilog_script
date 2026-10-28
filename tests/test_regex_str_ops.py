"""String-op replacements for hot regexes: behaviour pins.

Each conversion here replaces a compiled regex with a C-speed string
operation plus (where the equivalence has an exotic-input edge) a regex
fallback.  The tests pin the exact semantics, including the edges that
justify the fallback — so a future "simplification" that drops the
fallback fails loudly instead of silently changing behaviour.
"""

from verilog_tooling.autodef import _eval_bound, _is_int_literal
from verilog_tooling.fmt import _inst_pin_parts, _split_line_comment
from verilog_tooling.libdirs import (
    parse_align_typedef_words,
    parse_typedef_regexp,
)


def test_is_int_literal_ascii():
    assert _is_int_literal("5")
    assert _is_int_literal("-5")
    assert _is_int_literal("0")
    assert not _is_int_literal("")
    assert not _is_int_literal("-")
    assert not _is_int_literal("--5")
    assert not _is_int_literal("+5")
    assert not _is_int_literal("5 ")
    assert not _is_int_literal("12a")
    assert not _is_int_literal("1_000")


def test_is_int_literal_unicode_fallback():
    # \d matches Unicode decimal digits; isdigit alone would also accept
    # superscripts, so non-ASCII must go through the regex, not the fast
    # path: Arabic-Indic 3 is a digit, superscript 2 is not.
    assert _is_int_literal("\u0663")
    assert _is_int_literal("\uff15")  # fullwidth 5, Nd category
    assert not _is_int_literal("\u00b2")


def test_eval_bound_uses_fast_paths():
    assert _eval_bound("42", {}) == 42
    assert _eval_bound(" 42 ", {}) == 42
    assert _eval_bound("-7", {}) == -7
    assert _eval_bound("2*3+1", {}) == 7
    assert _eval_bound("a + b", {}, symbolic=True) == "a+b"
    assert _eval_bound("5", {}, symbolic=True) == "5"
    assert _eval_bound("\u0663", {}) == 3  # Unicode digit via fallback


def test_libdirs_typedef_positive_case():
    lines = [
        "module top;",
        "// Local Variables:",
        '// verilog-align-typedef-words: ("svi" "axi_t")',
        '// verilog-typedef-regexp:"my_t"',
        "// End:",
        "endmodule",
    ]
    assert parse_align_typedef_words(lines) == ["svi", "axi_t"]
    assert parse_typedef_regexp(lines) == r"my_t|\bsvi\b|\baxi_t\b"


def test_libdirs_typedef_words_only():
    lines = [
        "module top;",
        '// verilog-align-typedef-words: ("svi")',
        "endmodule",
    ]
    assert parse_typedef_regexp(lines) == r"\bsvi\b"


def test_libdirs_typedef_decoy_lines_ignored():
    # The literal alone is not a match: the guard must still run the
    # regex, and the regex must reject a line without the `: (...)` /
    # `:"..."` shape.
    lines = [
        "// note: verilog-align-typedef-words has no parens here",
        "// note: verilog-typedef-regexp has no quotes here",
    ]
    assert parse_align_typedef_words(lines) == []
    assert parse_typedef_regexp(lines) is None
    assert parse_align_typedef_words(["module m;", "endmodule"]) == []
    assert parse_typedef_regexp(["module m;", "endmodule"]) is None


def test_split_line_comment():
    assert _split_line_comment("a // b") == ("a ", "// b")
    assert _split_line_comment("a") == ("a", "")
    assert _split_line_comment("//x") == ("", "//x")
    assert _split_line_comment("a //b //c") == ("a ", "//b //c")
    # `.` excludes `\n`: the comment ends at the newline, which stays
    # in the body — exactly like the `//.*` regex.
    assert _split_line_comment("a // b\nc") == ("a \nc", "// b")


def test_inst_pin_parts_keeps_trailing_comment():
    port, conn, term, comment, tail = _inst_pin_parts(".clk (clk), // comment")
    assert (port, conn, term, comment, tail) == ("clk", "clk", ",", "// comment", "")
    port, conn, term, comment, tail = _inst_pin_parts(".clk (clk),")
    assert (port, conn, term, comment, tail) == ("clk", "clk", ",", "", "")
    assert _inst_pin_parts("  // .clk (clk),") is None
