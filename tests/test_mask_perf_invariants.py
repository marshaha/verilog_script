"""Equivalence invariants for the mask_comments perf work (455af1f).

That commit stops re-masking ``text[:close+1]`` per call and instead reuses
one ``mask_comments(text)`` result (``masked=`` parameters), and it
short-circuits ``strip_line_comments`` on plain single-line text.  Both are
only valid because of the exact equivalences pinned below; if a future
change drifts the masker semantics, these fail loudly instead of silently
corrupting paren balance, parameter reads, or instance resolution.

Style notes: no hypothesis dependency (repo floor is Python 3.7), seeded
``random.Random`` only, plain asserts.
"""

import random

from verilog_tooling import emacs
from verilog_tooling.comments import mask_comments, strip_line_comments

_SEED = 0x455AF1F % (2 ** 31)


def _rng(seed):
    return random.Random(seed)


# A token soup that exercises every masker state: both comment styles
# (including unbalanced parens inside comments — the exact hazard the
# masker exists for), strings with escapes, and Verilog paren/hash syntax.
_TOKENS = [
    "(", ")", "#", ".", ",", ";", " ", "  ", "\t", "\n",
    "//", "// unbalanced ) here", "// /* not a block",
    "/*", "*/", "/* unbalanced ( here */", "/* // still comment */",
    '"', '"a//b"', '"p/*q"', '"esc \\" )"', '""',
    "moda", "sub_sys", "u1", "u_inst_2", ".clk", ".din", "32'hff",
    "AUTOINST", "/*autoinst*/", "`define", "W", "8", "-1", "+:",
]


def _fuzz_text(rng, n):
    return "".join(rng.choice(_TOKENS) for _ in range(n))


def _slow_strip_line_comments(text):
    # The pre-455af1f implementation, kept as the reference oracle.
    return mask_comments(text, block=False, cut_line=True)


def test_mask_prefix_identity_after_rparen():
    """mask_comments(t)[:c+1] == mask_comments(t[:c+1]) when t[c] == ')'.

    This is the exact invariant _skip_group_back's mask reuse relies on:
    its prefix always ends right after a ')'.  (Arbitrary cut points do
    NOT share the identity: a cut splitting '/*' in two makes the prefix
    scan miss the opener.  ')' can neither open nor continue a two-char
    token, so the two scans agree on every position <= c.)
    """
    rng = _rng(_SEED)
    checked = 0
    for trial in range(200):
        text = _fuzz_text(rng, rng.randint(0, 60))
        full = mask_comments(text)
        assert len(full) == len(text)
        for c in [i for i, ch in enumerate(text) if ch == ")"]:
            assert full[: c + 1] == mask_comments(text[: c + 1]), \
                (trial, c, text)
            checked += 1
    assert checked > 200, "fuzzer produced too few ')' cut points"


def test_mask_prefix_identity_fails_mid_token():
    """Document the boundary: cutting inside '/*' breaks the identity.

    Guards the commit comment's "any prefix" phrasing — the reuse is only
    sound because _skip_group_back cuts after ')'.
    """
    text = "sub_sys /* comment */ x"
    c = text.index("/*") + 1  # cut between '/' and '*'
    assert mask_comments(text)[:c] != mask_comments(text[:c])


def test_mask_idempotent():
    """mask_comments(mask_comments(t)) == mask_comments(t).

    inst._statement_span passes already-masked text AS the mask; that is
    only sound because masking is a fixed point.
    """
    rng = _rng(_SEED + 1)
    for trial in range(120):
        text = _fuzz_text(rng, rng.randint(0, 60))
        once = mask_comments(text)
        assert mask_comments(once) == once, (trial, text)


def test_strip_line_comments_fast_path_parity():
    """Single-line text without strings or /* */: fast path == slow path."""
    rng = _rng(_SEED + 2)
    tokens = [t for t in _TOKENS if "\n" not in t and '"' not in t
              and "/*" not in t]
    hit_fast = 0
    for trial in range(400):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(0, 12)))
        got = strip_line_comments(text)
        assert got == _slow_strip_line_comments(text), (trial, text)
        if "//" in text:
            hit_fast += 1
    assert hit_fast > 100, "fuzzer never exercised the fast path"


def test_strip_line_comments_general_parity():
    """Arbitrary text (slow path included): result always == reference."""
    rng = _rng(_SEED + 3)
    for trial in range(400):
        text = _fuzz_text(rng, rng.randint(0, 40))
        assert strip_line_comments(text) == _slow_strip_line_comments(text), \
            (trial, text)


def test_skip_group_back_mask_reuse():
    """_skip_group_back(t, i, mask) == _skip_group_back(t, i) for all i."""
    rng = _rng(_SEED + 4)
    for trial in range(60):
        text = _fuzz_text(rng, rng.randint(1, 50))
        masked = mask_comments(text)
        for i in range(len(text) + 1):
            try:
                want = emacs._skip_group_back(text, i)
            except ValueError:
                want = "RAISED"
            try:
                got = emacs._skip_group_back(text, i, masked)
            except ValueError:
                got = "RAISED"
            assert got == want, (trial, i, text)


_NOISE = [
    " ", "\n", "\t", "// c\n", "// unbalanced ) )\n",
    "/* x */", "/* unbalanced ( ( */",
]


def _fuzz_instance(rng):
    """Build 'mod #(.P(v),...) u ( .a(x), ... );' with noise between parts,
    returning (text, pin_open_offset)."""
    mod = rng.choice(["moda", "sub_sys", "fifo_w32"])
    inst = rng.choice(["u1", "u_dut", "genblk1_u2"])
    np = rng.randint(0, 3)
    params = [".P%d(%s)" % (k, rng.choice(["8", "32", '"s//t"', "W-1"]))
              for k in range(np)]
    npins = rng.randint(1, 4)
    pins = [".p%d(%s)" % (k, rng.choice(["clk", "{a, b}", "sig%d" % k,
                                        '"x//y"']))
            for k in range(npins)]

    def noise(n=2):
        return "".join(rng.choice(_NOISE) for _ in range(rng.randint(0, n)))

    chunks = [noise(), mod]
    if params:
        chunks += [noise(), "#", noise(), "(",
                   (", ".join(params)), ")"]
    chunks += [noise(), inst, noise()]
    text = "".join(chunks)
    pin_open = len(text)
    chunks += ["(", ", ".join(pins), ")", noise(), ";"]
    return "".join(chunks), pin_open


def test_read_inst_param_values_mask_reuse():
    rng = _rng(_SEED + 5)
    for trial in range(200):
        text, pin_open = _fuzz_instance(rng)
        masked = mask_comments(text)
        want = emacs.read_inst_param_values(text, pin_open)
        got = emacs.read_inst_param_values(text, pin_open, masked)
        assert got == want, (trial, text)


def test_resolve_instance_at_mask_reuse():
    rng = _rng(_SEED + 6)
    for trial in range(200):
        text, pin_open = _fuzz_instance(rng)
        masked = mask_comments(text)
        try:
            want = emacs._resolve_instance_at(text, pin_open)
        except ValueError:
            want = "RAISED"
        try:
            got = emacs._resolve_instance_at(text, pin_open, masked)
        except ValueError:
            got = "RAISED"
        assert got == want, (trial, text)
