"""Python rewrite of automatic.vim's AutoModule/AutoPara/AutoFsm commands.

Covers the code-generation family bound in ~/.vimrc:

- ``auto_module``        <- AutoModule      (AM,  automatic.vim 7718-7748)
- ``auto_module_emacs``  <- AutoModuleEmacs (AME, automatic.vim 7680-7716)
- ``auto_para``          <- AutoPara        (APM, automatic.vim 4651-4696)
- ``kill_auto_para``     <- KillAutoPara    (     automatic.vim 4590-4618)
- ``auto_fsm``           <- AutoFsm         (AFM, automatic.vim 4735-4839)
- ``kill_auto_fsm``      <- KillAutoFsm     (     automatic.vim 4705-4733)

``auto_module``/``auto_module_emacs`` turn the word under the cursor into an
instance stub ``fifo  u0_fifo(/*autoinst*/);``; the buffer-wide commands
expand ``/*autopara*/ (A, B=2, C)`` into ``parameter`` declarations and
``/*autofsm*/ (IDLE,RUN,DONE) state nstate`` into localparams plus the
two-always-block FSM skeleton.

The buffer-facing commands live on :class:`~verilog_tooling.inst.VerilogBuffer`
(attached on import, like :mod:`verilog_tooling.fmt`); the module-level
functions are pure ``lines in -> lines out`` wrappers (single-line transform
for AM/AME).

Quirks preserved from the Vim originals (all verified against real Vim 9.1):

- AM/AME count *every* prior buffer line whose first two whitespace-separated
  tokens exist and whose first token equals the module name — including the
  cursor line itself — so a bare ``fifo`` becomes ``u0_fifo`` while a
  parameterised ``fifo #(W=8)`` becomes ``u1_fifo`` (the line matches its own
  instance pattern before being replaced).
- AutoPara/AutoFsm use ``s:CalMargin``'s ``>=`` comparison, so a name exactly
  as long as the longest still earns one trailing space (unlike
  :func:`~verilog_tooling.inst._cal_margin`'s strict ``>``).
- para_wid is the smallest w with ``2**w - 1 >= max`` numeric value (Vim's
  ``pow()`` loop; verified for max 0..15: 7 -> 3 bits, 8 -> 4 bits).
- AutoFsm requires the state list without inner spaces: Vim splits the whole
  rest of the marker line on whitespace and takes token 0 as the state list,
  so ``(IDLE, RUN, DONE)`` is mangled into the single bogus state ``RUN,``.
  This port parses the ``(...)`` group properly and documents the deviation.
- The default next-state name keeps Vim's trailing space: ``next_state ``
  becomes ``next_state [1:0]`` after the width is appended.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence

from .inst import VerilogBuffer

_INDENT = " " * 4  # s:indent (s:vlog_ind = 4)

_AUTOPARA_MARK = re.compile(r"/\*\s*\bautopara\b")
_AUTOFSM_MARK = re.compile(r"/\*\s*\bautofsm\b")
_ENDMODULE = re.compile(r"^\s*\bendmodule\b")
_INST_LINE = re.compile(r"^\s*\w+\s+\S+")
_FIRST_WORD = re.compile(r"^\s*\w+")
_OTHERS = re.compile(r"\w\s+.*")


# ---------------------------------------------------------------------------
# AutoModule / AutoModuleEmacs (single-line transforms)


def _module_and_others(line: str) -> tuple[str, str]:
    """Vim's matchstr pair: first word, and the rest with all whitespace
    removed and the leading word replaced by two spaces."""
    m = _FIRST_WORD.match(line)
    module_name = m.group(0).strip() if m else ""
    om = _OTHERS.search(line)
    others = om.group(0) if om else ""
    others = re.sub(r"^\w", "  ", others)
    others = re.sub(r"\s", "", others)
    return module_name, others


def _inst_count(lines: Sequence[str], module_name: str) -> int:
    """Count buffer lines whose first word is MODULE_NAME and which hold at
    least a second token (Vim's instance-count loop; counts the cursor line
    itself when it already has a parameter/second word)."""
    cnt = 0
    for line in lines:
        m = _INST_LINE.match(line)
        if not m:
            continue
        fm = _FIRST_WORD.match(m.group(0))
        if fm and fm.group(0).strip() == module_name:
            cnt += 1
    return cnt


def auto_module(lines: Sequence[str], line_idx: int) -> list[str]:
    """AM: replace LINES[LINE_IDX] (0-based) with an instance stub.

    ``fifo`` -> ``fifo  u0_fifo(/*autoinst*/);``; any ``#(...)`` text after
    the module name is kept (whitespace-stripped) before the instance name.
    """
    out = list(lines)
    module_name, others = _module_and_others(out[line_idx])
    inst_cnt = _inst_count(out, module_name)
    inst_name = f"  u{inst_cnt}_{module_name}"
    out[line_idx] = module_name + others + inst_name + "(/*autoinst*/);"
    return out


def auto_module_emacs(lines: Sequence[str], line_idx: int) -> list[str]:
    """AME: like AM, plus prepend a ``/* module  auto_template ( ); */``
    template block; a bare module name gains ``#(/*autoinstparam*/)``."""
    out = list(lines)
    module_name, others = _module_and_others(out[line_idx])
    inst_cnt = _inst_count(out, module_name)
    if others == "":
        others = " #(/*autoinstparam*/) "
    inst_name = f"  u{inst_cnt}_{module_name}"
    new_line = module_name + others + inst_name + "(/*autoinst*/);"
    out[line_idx : line_idx + 1] = [
        f"/* {module_name}  auto_template ( ",
        "  ); */",
        new_line,
    ]
    return out


# ---------------------------------------------------------------------------
# s:GetAutoParas / parameter helpers


def _get_auto_paras(autopara_text: str) -> list[tuple[str, object]]:
    """Parse ``(A, B=2, C)`` into [(name, value)] with default threading.

    VALUE is an int for numeric entries and a str for symbolic ones (``= X``
    or the ``name+1`` style running default).  Mirrors s:GetAutoParas: a
    bare name takes the running default (starting at 0) and bumps it; ``= N``
    sets the default to N+1; ``= name`` sets the default to ``name+1``.
    """
    m = re.search(r"\(.*\)", autopara_text)
    text = m.group(0) if m else autopara_text
    text = text.replace("(", "").replace(")", "")
    paras: list[tuple[str, object]] = []
    default: object = 0
    for entry in text.split(","):
        vm = re.search(r"=.*$", entry)
        cur = re.sub(r"=\s*", "", vm.group(0)) if vm else ""
        if cur == "":
            value = default
            if isinstance(default, str) and re.search(r"[a-zA-Z]", default):
                # bump the trailing number of a symbolic default: X+1 -> X+2
                nm = re.search(r"\d+", default)
                bumped = str(int(nm.group(0)) + 1) if nm else "1"
                default = re.sub(r"\d+", "", default) + bumped
            else:
                default = int(default) + 1  # type: ignore[arg-type]
        elif re.search(r"[a-zA-Z]", cur):
            value = cur
            default = cur + "+1"
        else:
            value = int(cur)
            default = int(cur) + 1
        name = re.sub(r"^\s*", "", entry)
        name = re.sub(r"\s*=.*$", "", name)
        name = re.sub(r"\s*$", "", name)
        paras.append((name, value))
    return paras


def _para_width(paras: Sequence[tuple[str, object]]) -> int:
    """Smallest w with ``2**w - 1 >= maxpara`` (str2nr of each value).

    Verified against Vim for single- and multi-parameter lists
    (0->1, 1->1, 2->2, 3->2, 7->3, 8->4, 9->4, 15->4).
    """
    maxpara = 0
    for _, value in paras:
        try:
            maxpara = max(maxpara, int(str(value)))
        except ValueError:
            pass  # str2nr('X+1') == 0 in Vim
    w = 1
    while (2**w - 1) < maxpara:
        w += 1
    return w


def _cal_margin_ge(max_len: int, cur_len: int) -> str:
    """automatic.vim s:CalMargin (``>=`` variant used by AutoPara/AutoFsm):
    pads to max_len+1 even when cur_len == max_len."""
    return " " * (max_len - cur_len + 1) if max_len >= cur_len else ""


def _para_lines(
    paras: Sequence[tuple[str, object]], keyword: str
) -> list[str]:
    maxlen = max((len(name) for name, _ in paras), default=0)
    para_wid = _para_width(paras)
    out = []
    for name, value in paras:
        margin = _cal_margin_ge(maxlen, len(name))
        out.append(f"{keyword} {name}{margin}= {para_wid}'d{value};")
    return out


# ---------------------------------------------------------------------------
# KillAutoPara / AutoPara


def _kill_region(lines: Sequence[str], start: str, end_pat: str) -> list[str]:
    """KillAutoPara/KillAutoFsm shared shape: drop every region from a line
    exactly equal to START through the line matching END_PAT (inclusive),
    stopping at ``endmodule``."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line == start:
            i += 1
            while i < n - 1 and not re.search(end_pat, lines[i]):
                i += 1
            i += 1  # drop the end line too (Vim's while stops AT it, then +1)
        elif _ENDMODULE.match(line):
            out.append(line)
            break
        else:
            out.append(line)
            i += 1
    return out


def kill_auto_para(lines: Sequence[str]) -> list[str]:
    """KillAutoPara: drop ``// Define parameter here`` ... ``// End of
    automatic parameter`` regions (the marker line itself is kept)."""
    return _kill_region(lines, "// Define parameter here", r"// End of automatic parameter")


def auto_para(lines: Sequence[str]) -> list[str]:
    """APM: expand every ``/*autopara*/ (A, B=2, C)`` marker into aligned
    ``parameter`` declarations between the Define/End markers.

    Regenerates idempotently (KillAutoPara runs first); lines after
    ``endmodule`` are left untouched, as in the Vim original.
    """
    lines = kill_auto_para(lines)
    out: list[str] = []
    for line in lines:
        if _AUTOPARA_MARK.search(line):
            out.append(line)
            out.append("// Define parameter here")
            rest = re.sub(r"^.*\bautopara\b\s*\*/\s*", "", line)
            rest = re.sub(r"^\W+", "", rest)
            out.extend(_para_lines(_get_auto_paras("(" + rest + ")"), "parameter"))
            out.append("// End of automatic parameter")
        elif _ENDMODULE.match(line):
            out.append(line)
            break
        else:
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# KillAutoFsm / AutoFsm


def kill_auto_fsm(lines: Sequence[str]) -> list[str]:
    """KillAutoFsm: drop ``// Define fsm here`` ... ``// End of automatic
    fsm`` regions (the marker line itself is kept)."""
    return _kill_region(lines, "// Define fsm here", r"// End of automatic fsm")


def _fix_width(name: str, sta_width: int) -> str:
    """Vim's width fixup: strip from the first ``[`` on, append [sta_width:0]."""
    name = re.sub(r"\[.*", "", name)
    return f"{name}[{sta_width}:0]"


def auto_fsm(lines: Sequence[str]) -> list[str]:
    """AFM: expand every ``/*autofsm*/ (IDLE,RUN,DONE) state nstate`` marker
    into FSM localparams plus the two-always-block skeleton.

    Unlike the Vim original (which whitespace-splits the whole rest of the
    marker line and mangles space-separated state lists), the state list is
    taken from the ``(...)`` group and the following words are the state and
    next-state signal names; the default next-state name keeps Vim's trailing
    space (``next_state [1:0]``).
    """
    lines = kill_auto_fsm(lines)
    out: list[str] = []
    for line in lines:
        if not _AUTOFSM_MARK.search(line):
            out.append(line)
            if _ENDMODULE.match(line):
                break
            continue
        out.append(line)
        out.append("// Define fsm here")
        rest = re.sub(r"^.*\bautofsm\b\s*\*/\s*", "", line)
        sm = re.search(r"\(([^)]*)\)", rest)
        if sm:
            # explicit parenthesised list: "(IDLE,RUN,DONE) state [nstate]"
            state = sm.group(1)
            tail = rest[sm.end() :]
        else:
            # bare list behind a comment: "// P0,P1,P2,P3 cur_sta nxt_sta"
            rest = re.sub(r"^\W+", "", rest)  # drop leading "//" comment marker
            m2 = re.match(r"([A-Za-z0-9_,]+)(?:\s+(.*))?", rest)
            state = m2.group(1) if m2 else ""
            tail = (m2.group(2) or "") if m2 else ""
        words = tail.split()
        inst_state = words[0] if words else ""
        nxt_sta = words[1] if len(words) > 1 else "next_" + inst_state + " "
        paras = _get_auto_paras("(" + state + ")")

        out.append("// Define FSM parameter here")
        out.extend(_para_lines(paras, "localparam"))
        out.append("// End of automatic parameter for FSM")

        sta_width = _para_width(paras) - 1
        inst_state = _fix_width(inst_state, sta_width)
        nxt_sta = _fix_width(nxt_sta, sta_width)
        first_st = paras[0][0] if paras else ""

        out.append("always @(posedge clk or negedge rst_n) begin")
        out.append(_INDENT + "if(!rst_n) begin")
        out.append(_INDENT * 2 + inst_state + " <= #`RD " + first_st + ";")
        out.append(_INDENT + "end else begin")
        out.append(_INDENT * 2 + inst_state + " <= #`RD " + nxt_sta + ";")
        out.append(_INDENT + "end")
        out.append("end")

        out.append("always @(*) begin")
        out.append(_INDENT + nxt_sta + " = " + inst_state + ";")
        out.append(_INDENT + "case(" + inst_state + ")")
        for name, _ in paras:
            out.append(_INDENT * 2 + name + ": begin")
            out.append(_INDENT * 2 + "end")
        out.append(_INDENT * 2 + "default: begin")
        out.append(_INDENT * 2 + "end")
        out.append(_INDENT + "endcase")
        out.append("end")

        out.append("// End of automatic fsm")
    return out


# ---------------------------------------------------------------------------
# VerilogBuffer methods (attached on import, like verilog_tooling.fmt)


def _auto_module_buf(self: VerilogBuffer, line_idx: int) -> VerilogBuffer:
    """AM: turn the word on LINE_IDX (0-based) into an instance stub."""
    return VerilogBuffer(auto_module(self._lines, line_idx))


def _auto_module_emacs_buf(self: VerilogBuffer, line_idx: int) -> VerilogBuffer:
    """AME: like AM, plus a leading auto_template comment block."""
    return VerilogBuffer(auto_module_emacs(self._lines, line_idx))


def _auto_para_buf(self: VerilogBuffer) -> VerilogBuffer:
    """APM: expand every /*autopara*/ marker into parameter declarations."""
    return VerilogBuffer(auto_para(self._lines))


def _kill_auto_para_buf(self: VerilogBuffer) -> VerilogBuffer:
    """KillAutoPara: collapse generated parameter regions."""
    return VerilogBuffer(kill_auto_para(self._lines))


def _auto_fsm_buf(self: VerilogBuffer) -> VerilogBuffer:
    """AFM: expand every /*autofsm*/ marker into an FSM skeleton."""
    return VerilogBuffer(auto_fsm(self._lines))


def _kill_auto_fsm_buf(self: VerilogBuffer) -> VerilogBuffer:
    """KillAutoFsm: collapse generated FSM regions."""
    return VerilogBuffer(kill_auto_fsm(self._lines))


VerilogBuffer.auto_module = _auto_module_buf
VerilogBuffer.auto_module_emacs = _auto_module_emacs_buf
VerilogBuffer.auto_para = _auto_para_buf
VerilogBuffer.kill_auto_para = _kill_auto_para_buf
VerilogBuffer.auto_fsm = _auto_fsm_buf
VerilogBuffer.kill_auto_fsm = _kill_auto_fsm_buf


# ---------------------------------------------------------------------------
# CLI (mirrors verilog_tooling.inst so Vim can call it the same way)


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.gen",
        description="automatic.vim AM/AME/APM/AFM (+ kills) rewrite",
    )
    parser.add_argument(
        "command",
        choices=["am", "ame", "apm", "afm", "kill-para", "kill-fsm"],
        help="am/ame: AutoModule/AutoModuleEmacs; apm/afm: AutoPara/AutoFsm; "
        "kill-para/kill-fsm: KillAutoPara/KillAutoFsm",
    )
    parser.add_argument("-i", "--in_file", required=True, help="buffer file")
    parser.add_argument("-o", "--out_file", required=True, help="output file")
    parser.add_argument(
        "--line",
        type=int,
        default=None,
        help="1-based line number for am/ame (0 = first word-bearing line)",
    )
    # accepted and ignored, so the shared Vim front-end can always pass them
    parser.add_argument("-y", "--libdir", action="append", default=[],
                        help="(unused by these commands)")
    parser.add_argument("--ref_file", default=None,
                        help="(unused by these commands)")
    return parser.parse_args(args_l)


def _resolve_line(lines: Sequence[str], line: int | None, command: str) -> int:
    if line is None:
        raise SystemExit(f"--line is required for {command}")
    if line == 0:
        for i, text in enumerate(lines):
            if _FIRST_WORD.match(text):
                return i
        raise SystemExit("no word-bearing line found")
    idx = line - 1
    if not 0 <= idx < len(lines):
        raise SystemExit(f"--line {line} out of range (buffer has {len(lines)} lines)")
    return idx


def main(argv=None) -> None:
    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()
    if args.command == "am":
        out = auto_module(lines, _resolve_line(lines, args.line, "am"))
    elif args.command == "ame":
        out = auto_module_emacs(lines, _resolve_line(lines, args.line, "ame"))
    elif args.command == "apm":
        out = auto_para(lines)
    elif args.command == "afm":
        out = auto_fsm(lines)
    elif args.command == "kill-para":
        out = kill_auto_para(lines)
    else:
        out = kill_auto_fsm(lines)
    Path(args.out_file).write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
