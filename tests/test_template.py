"""Week 4: verilog-mode AUTO_TEMPLATE regex features (template.py)."""

import pytest

from verilog_tooling.template import (
    expand_connection,
    find_auto_templates,
    template_at_value,
    template_connection,
    template_for_module,
)


def test_literal_template_and_at_from_first_digits():
    text = """
/* InstModule AUTO_TEMPLATE (
    .ptl_mapvalidx  (ptl_mapvalid[@]),
    .cfg            ({cfg_hi[@], cfg_lo[@]}),
); */
"""
    (t,) = find_auto_templates(text)
    assert t.modules == ("InstModule",)
    assert t.at_regexp is None
    # verilog-mode takes the FIRST digits of the instance name
    assert template_at_value(t, "ms2m") == "2"
    assert template_connection(t, "ptl_mapvalidx", "2", None) == "ptl_mapvalid[2]"
    assert template_connection(t, "cfg", "2", "3:0") == "{cfg_hi[2], cfg_lo[2]}"
    assert template_connection(t, "other", "2", None) is None
    assert not t.entries[0].is_regex


def test_custom_at_regexp():
    text = '/* M AUTO_TEMPLATE "_\\([a-z]+\\)" (\n    .sigx (@_sig),\n); */'
    (t,) = find_auto_templates(text)
    assert t.at_regexp == r"_\([a-z]+\)"
    # verilog-mode matches the @ regexp case-insensitively
    assert template_at_value(t, "ms2_FOO") == "FOO"
    assert template_connection(t, "sigx", "FOO", None) == "FOO_sig"


def test_regexp_port_pattern_with_backref():
    text = r"""/* M AUTO_TEMPLATE (
    .pci_req\([0-9]+\)_l      (pci_req_jtag_[\1]),
); */"""
    (t,) = find_auto_templates(text)
    assert t.entries[0].is_regex
    assert template_connection(t, "pci_req2_l", "", None) == "pci_req_jtag_[2]"
    assert template_connection(t, "pci_req10_l", "", None) == "pci_req_jtag_[10]"
    # a port the regexp does not match falls back to identity (None here)
    assert template_connection(t, "other", "", None) is None


def test_at_inside_port_pattern_is_a_digit_group():
    text = "/* M AUTO_TEMPLATE (\n    .in@ (out[\\1]),\n); */"
    (t,) = find_auto_templates(text)
    assert t.entries[0].is_regex
    assert template_connection(t, "in3", "", None) == "out[3]"
    assert template_connection(t, "inx", "", None) is None


def test_empty_brackets_expand_to_port_range_or_vanish():
    text = "/* M AUTO_TEMPLATE (\n    .bus (net[]),\n); */"
    (t,) = find_auto_templates(text)
    assert template_connection(t, "bus", "", "7:0") == "net[7:0]"
    assert template_connection(t, "bus", "", None) == "net"


def test_exact_entry_beats_regexp_and_last_entry_wins():
    text = r"""/* M AUTO_TEMPLATE (
    .a\([0-9]+\) (wild[\1]),
    .a0          (exact0),
); */"""
    (t,) = find_auto_templates(text)
    assert template_connection(t, "a0", "", None) == "exact0"
    assert template_connection(t, "a7", "", None) == "wild[7]"
    text = "/* M AUTO_TEMPLATE (\n    .x (first),\n    .x (second),\n); */"
    (t,) = find_auto_templates(text)
    assert template_connection(t, "x", "", None) == "second"


def test_lisp_expression_raises():
    text = '/* M AUTO_TEMPLATE (\n    .sig (sigy[@"(% (+ 1 @) 4)"]),\n); */'
    (t,) = find_auto_templates(text)
    # @"(...)" is now evaluated as a Python expression; the elisp form raises
    with pytest.raises(ValueError, match="evaluation failed"):
        template_connection(t, "sig", "2", None)


def test_lisp_expression_python_subset():
    text = '/* M AUTO_TEMPLATE (\n    .sig (sigy[@"(%d % (@ + 1) % 4)"]),\n); */'
    # Python format-string inside @"..." evaluates to the computed index
    (t,) = find_auto_templates('/* M AUTO_TEMPLATE (\n    .sig (sigy[@"@+1"]),\n); */')
    assert template_connection(t, "sig", "2", None) == "sigy[3]"


def test_multiple_module_template_header():
    text = """/* InstModuleA AUTO_TEMPLATE
   InstModuleB AUTO_TEMPLATE
   InstModuleC AUTO_TEMPLATE (
    .ptl_bus (ptl_busnew[]),
); */"""
    (t,) = find_auto_templates(text)
    assert t.modules == ("InstModuleA", "InstModuleB", "InstModuleC")
    assert template_connection(t, "ptl_bus", "", "3:0") == "ptl_busnew[3:0]"


def test_template_for_module_closest_above_wins():
    text = (
        "/* M AUTO_TEMPLATE (\n    .a (upper),\n); */\n"
        "wire w;\n"
        "/* M AUTO_TEMPLATE (\n    .a (lower),\n); */\n"
        "M u_m_0 (/*autoinst*/);\n"
    )
    templates = find_auto_templates(text)
    assert len(templates) == 2
    marker_line = text.splitlines().index("M u_m_0 (/*autoinst*/);")
    t = template_for_module(templates, "M", before_line=marker_line)
    assert template_connection(t, "a", "", None) == "lower"
    assert template_for_module(templates, "other", before_line=marker_line) is None


def test_no_digits_in_instance_name_gives_empty_at():
    (t,) = find_auto_templates("/* M AUTO_TEMPLATE (\n    .a (sig[@]),\n); */")
    assert template_at_value(t, "u_no_num") == ""
    # @ -> '' leaves sig[]; [] then expands to the port range (or vanishes)
    assert template_connection(t, "a", "", "3:0") == "sig[3:0]"
    assert template_connection(t, "a", "", None) == "sig"


def test_template_without_module_name_raises():
    with pytest.raises(ValueError, match="module name"):
        from verilog_tooling.template import _parse_template_body

        _parse_template_body(" AUTO_TEMPLATE (\n    .a (b),\n); ", 0)


def test_malformed_template_skipped_with_warning(capsys):
    """A malformed block (unbalanced parens / no module name) is skipped
    with a warning — it must not abort the whole command."""
    out = find_auto_templates(
        "/* AUTO_TEMPLATE (\n    .a (b),\n); */\n"
        "/* sub AUTO_TEMPLATE (\n    .c (d),\n); */"
    )
    assert [t.modules for t in out] == [("sub",)]
    assert "malformed AUTO_TEMPLATE" in capsys.readouterr().err


def test_expand_connection_elisp_forms():
    # elisp arithmetic: (+ 1 @) with @=2 -> 3
    assert expand_connection('sig[@"(+ 1 @)"]', "2", None) == "sig[3]"
    # substring/downcase on vl-name (elisp form)
    assert (
        expand_connection('inner_@"(substring vl-name 2)"[]', "0", "7:0", None, vl_name="m_AWADDR")
        == "inner_AWADDR[7:0]"
    )
    assert (
        expand_connection('x_@"(downcase (substring vl-name 2))"', "0", None, None, vl_name="m_BUS")
        == "x_bus"
    )
    # vl-cell-name (wildcell/instname style)
    assert (
        expand_connection(
            '@"(substring vl-cell-name 4 5)"', "0", None, None, vl_cell_name="u0_mm_sfifo"
        )
        == "m"
    )


def test_expand_connection_python_at_expr():
    assert expand_connection('sig[@"@+1"]', "2", None) == "sig[3]"
    # env from AUTO_LISP
    assert expand_connection('sig[@"base + @"]', "2", None, {"base": 10}) == "sig[12]"


# ---------------------------------------------------------------------------
# dual-dialect regexps: Emacs \( \) \| and Python ( ) | are auto-detected


def test_python_dialect_port_pattern_with_backref():
    text = r"""/* M AUTO_TEMPLATE (
    .cdma_(ar|aw)id_s      (cdma_\1id_internal[1:0]),
); */"""
    (t,) = find_auto_templates(text)
    assert t.entries[0].is_regex
    assert template_connection(t, "cdma_arid_s", "", None) == "cdma_arid_internal[1:0]"
    assert template_connection(t, "cdma_awid_s", "", None) == "cdma_awid_internal[1:0]"
    assert template_connection(t, "cdma_wid_s", "", None) is None


def test_python_dialect_replacement_g_backref():
    text = r"""/* M AUTO_TEMPLATE (
    .pci_req([0-9]+)_l      (pci_req_jtag_[\g<1>]),
); */"""
    (t,) = find_auto_templates(text)
    assert template_connection(t, "pci_req7_l", "", None) == "pci_req_jtag_[7]"


def test_python_dialect_at_regexp():
    text = '/* M AUTO_TEMPLATE "_([a-z]+)" (\n    .sigx (@_sig),\n); */'
    (t,) = find_auto_templates(text)
    assert template_at_value(t, "ms2_FOO") == "FOO"
    assert template_connection(t, "sigx", "FOO", None) == "FOO_sig"


def test_mixed_dialect_pattern():
    # emacs alternation + python group in one pattern degrades gracefully
    text = r"""/* M AUTO_TEMPLATE (
    .pre_\(a\|b\)([0-9]+)      (x_\1_\2),
); */"""
    (t,) = find_auto_templates(text)
    assert template_connection(t, "pre_a3", "", None) == "x_a_3"
    assert template_connection(t, "pre_b10", "", None) == "x_b_10"


def test_typedef_regexp_both_dialects():
    from verilog_tooling.inst import parse_module_ports

    src = "module m (\n    input  mytype_t data,\n    input  clk\n);\nendmodule".splitlines()
    for rx in (r"\(_t\|_s\)$", r"(_t|_s)$"):  # emacs and python dialects
        mod = parse_module_ports(src, typedef_regexp=rx)
        names = [p.name for p in mod.ports]
        assert names == ["data", "clk"], rx


def test_regexp_entries_topmost_wins():
    """Overlapping wildcard entries: the TOP one wins (emacs re-assigns on
    every match while iterating bottom-up) — verified against real
    verilog-mode (sub/foobar -> TOP[7:0])."""
    text = r"""/* M AUTO_TEMPLATE (
    .foo.*     (TOP[]),
    .foob.*    (BOTTOM[]),
); */"""
    (t,) = find_auto_templates(text)
    assert template_connection(t, "foobar", "", "7:0") == "TOP[7:0]"


def test_exact_duplicates_bottom_wins():
    """Duplicate exact entries: the BOTTOM one wins (emacs assoc over the
    consed list) — verified against real verilog-mode (a0 -> exact_bottom)."""
    text = r"""/* M AUTO_TEMPLATE (
    .a0         (exact_top),
    .a0         (exact_bottom),
); */"""
    (t,) = find_auto_templates(text)
    assert template_connection(t, "a0", "", None) == "exact_bottom"
