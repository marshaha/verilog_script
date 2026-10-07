from verilog_tooling.parser import (
    Declaration,
    discover_for_scopes,
    expand_template,
    get_active_loop_vars,
    merge_assignments,
    normalize_lhs,
    parse_auto_template,
    parse_declaration,
    validate_array_arity,
)


def test_multidimensional_declaration():
    d = parse_declaration("wire [3:0][7:0] packed_bus [0:1][0:2];")
    assert d is not None
    assert d.packed == ("3:0", "7:0")
    assert d.unpacked == ("0:1", "0:2")


def test_nested_loop_scope_and_recovery():
    rtl = """always @(*) begin
    for (ch = 0; ch < CH_NUM; ch = ch + 1) begin
        acc[ch][0] = 16'd0;
        for (tap = 1; tap < TAP_NUM; tap = tap + 1) begin
            acc[ch][tap] = acc[ch][tap-1] + din[ch];
        end
        acc[ch][TAP_NUM-1] = acc[ch][TAP_NUM-1] + 1'b1;
    end
end""".splitlines()
    loops = discover_for_scopes(rtl)
    ch = next(x for x in loops if x.var == "ch")
    tap = next(x for x in loops if x.var == "tap")
    assert ch.scope[0] < tap.scope[0]
    assert tap.scope[1] < ch.scope[1]
    assert get_active_loop_vars(5, ["ch", "tap"], loops) == ["ch", "tap"]
    assert get_active_loop_vars(7, ["ch", "TAP_NUM-1"], loops) == ["ch"]


def test_lhs_normalization_and_assignment_merge():
    d = Declaration("data", "reg", ("15:0",), ("0:CH_NUM-1",))
    n = normalize_lhs("data[ch][15:8]", d)
    assert n == {
        "base": "data",
        "array_indices": ["ch"],
        "slice": "15:8",
        "target": "data[ch]",
    }
    merged = merge_assignments(
        [
            {"range": (3, 0)},
            {"range": (7, 4)},
            {"range": (15, 12)},
            {"range": (11, 8)},
        ],
        16,
    )
    assert merged["complete"] is True
    assert merged["overlap"] is False


def test_hole_and_overlap():
    hole = merge_assignments([{"range": (3, 0)}, {"range": (15, 8)}], 16)
    assert hole["missing_bits"] == {4, 5, 6, 7}
    overlap = merge_assignments([{"range": (7, 0)}, {"range": (5, 4)}], 8)
    assert overlap["overlap_bits"] == {4, 5}


def test_auto_template_and_expansion():
    text = r"""
/* proc AUTO_TEMPLATE (
    .din   (src[@]),
    .valid (valid_bus[@+1]),
    .cfg   ({cfg_hi[@], cfg_lo[@]}),
); */
"""
    t = parse_auto_template(text)
    assert t["module"] == "proc"
    ex = expand_template("u_proc_3", t["ports"])
    assert ex == {
        "din": "src[3]",
        "valid": "valid_bus[3+1]",
        "cfg": "{cfg_hi[3], cfg_lo[3]}",
    }


def test_multidimensional_template_arity():
    d = Declaration("matrix", "wire", ("15:0",), ("0:3", "0:7"))
    validate_array_arity("matrix[1][2]", d)
    try:
        validate_array_arity("matrix[1][2][1][0]", d)
    except ValueError:
        pass
    else:
        raise AssertionError("expected too-many-indices validation failure")
