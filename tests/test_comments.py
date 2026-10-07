"""comments.py: the state-machine comment masker (corner cases)."""

from verilog_tooling.comments import mask_comments, strip_comments, strip_line_comments


def test_line_comment_masked_offsets_preserved():
    text = "abc // def\nghi"
    assert mask_comments(text) == "abc       \nghi"
    assert len(mask_comments(text)) == len(text)


def test_block_comment_inline_and_spanning():
    assert mask_comments("a /* x */ b") == "a         b"
    text = "a /* x\ny */ b"
    assert mask_comments(text) == "a     \n     b"
    assert len(mask_comments(text)) == len(text)


def test_line_comment_inside_block_is_not_a_comment_start():
    # the classic ordering bug: stripping // first leaves an unbalanced /*
    text = "x /* // still comment */ y\nz"
    assert mask_comments(text) == "x                        y\nz"


def test_block_open_inside_line_comment_is_not_a_block():
    text = "// /* not a block\ny"
    assert mask_comments(text) == "                 \ny"


def test_comment_markers_inside_strings_are_kept():
    text = 'a = "x//y";\nb = "p/*q";\n// real'
    assert mask_comments(text) == 'a = "x//y";\nb = "p/*q";\n       '


def test_string_escaped_quote_and_backslash():
    text = 'a = "x\\" // not comment";\n// c'
    assert mask_comments(text) == 'a = "x\\" // not comment";\n    '


def test_attributes_are_not_comments():
    text = "(* keep = \"true\" *) input clk;"
    assert mask_comments(text) == text


def test_block_false_keeps_block_but_tracks_state():
    # template use case: only // comments masked, /* */ kept visible, and a
    # // inside the block is not masked
    text = "/* tpl // inner */ code // tail"
    assert mask_comments(text, block=False) == "/* tpl // inner */ code        "


def test_line_false_keeps_line_comments_but_tracks_state():
    text = "// keep /* no block\nnext"
    assert mask_comments(text, line=False) == "// keep /* no block\nnext"


def test_unterminated_block_masks_to_eof():
    assert mask_comments("a /* x\ny") == "a     \n "


def test_strip_comments_drop_in():
    assert strip_comments("abc // def\nghi /* x */ j") == "abc \nghi         j"


def test_division_and_adjacent_comment():
    text = "assign x = a / b; // div\nassign y = c//real comment\n"
    assert mask_comments(text) == "assign x = a / b;       \nassign y = c              \n"


def test_template_string_with_slashes():
    text = '.sig (@"pre_//_%d"),\n.foo (bar),'
    assert mask_comments(text) == '.sig (@"pre_//_%d"),\n.foo (bar),'



def test_strip_line_comments_naive_parity_with_cut():
    # naive re.sub(r"//.*$", "", line) parity: text before // kept verbatim
    assert strip_line_comments("abc // def") == "abc "
    assert strip_line_comments("a /* kept */ b // cut") == "a /* kept */ b "
    # but a // inside the block or a string is NOT a comment
    assert strip_line_comments('m = "x//y"; // tail') == 'm = "x//y"; '
    assert strip_line_comments("/* // */ code") == "/* // */ code"
