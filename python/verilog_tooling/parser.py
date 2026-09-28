from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_BRACKET = re.compile(r"\[([^\]]+)\]")


@dataclass(frozen=True)
class Declaration:
    name: str
    kind: str
    packed: tuple[str, ...]
    unpacked: tuple[str, ...]


@dataclass(frozen=True)
class LoopInfo:
    var: str
    init: str
    cond: str
    step: str
    scope: tuple[int, int]


def get_identifiers(expr: str) -> list[str]:
    return _IDENT.findall(expr)


def parse_declaration(line: str) -> Declaration | None:
    """Parse one simple Verilog declaration line.

    Supported shape:
      wire [7:0][3:0] foo [0:3][0:7];
      output reg [15:0] data [0:3];

    This intentionally stays conservative; it is a training scaffold rather
    than a complete SystemVerilog grammar.
    """
    s = re.sub(r"//.*$", "", line).strip().rstrip(";")
    m = re.match(r"^(?:(?:input|output|inout)\s+)?(wire|reg|logic)\s+(.+)$", s)
    if not m:
        return None
    kind, rest = m.groups()

    name_m = re.search(r"\b([A-Za-z_]\w*)\b(?=\s*(?:\[[^\]]+\]\s*)*$)", rest)
    if not name_m:
        return None
    name = name_m.group(1)
    before = rest[: name_m.start()]
    after = rest[name_m.end() :]
    packed = tuple(_BRACKET.findall(before))
    unpacked = tuple(_BRACKET.findall(after))
    return Declaration(name=name, kind=kind, packed=packed, unpacked=unpacked)


def parse_for_header(line: str) -> tuple[str, str, str, str] | None:
    m = re.search(
        r"for\s*\(\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*;\s*(.*?)\s*;\s*\1\s*=\s*(.*?)\s*\)",
        line,
    )
    if not m:
        return None
    var, init, cond, step = m.groups()
    return var, init.strip(), cond.strip(), step.strip()


def discover_for_scopes(lines: list[str]) -> list[LoopInfo]:
    """Discover for-loop scopes using begin/end depth.

    Assumes each relevant for-loop owns a begin/end block. Line numbers are
    1-based and include the for header through its matching end line.
    """
    loops: list[LoopInfo] = []
    stack: list[dict] = []
    depth = 0

    for lineno, line in enumerate(lines, start=1):
        header = parse_for_header(line)
        begin_count = len(re.findall(r"\bbegin\b", line))
        end_count = len(re.findall(r"\bend\b", line))

        if header:
            var, init, cond, step = header
            target_depth = depth + (1 if begin_count else 0)
            stack.append(
                {
                    "var": var,
                    "init": init,
                    "cond": cond,
                    "step": step,
                    "start": lineno,
                    "body_depth": target_depth,
                }
            )

        depth += begin_count
        depth -= end_count

        closed = [x for x in stack if depth < x["body_depth"]]
        for x in closed:
            loops.append(
                LoopInfo(
                    var=x["var"],
                    init=x["init"],
                    cond=x["cond"],
                    step=x["step"],
                    scope=(x["start"], lineno),
                )
            )
            stack.remove(x)

    return sorted(loops, key=lambda x: (x.scope[0], -x.scope[1]))


def get_active_loop_vars(line_no: int, indices: Iterable[str], loops: Iterable[LoopInfo]) -> list[str]:
    identifiers = set()
    for expr in indices:
        identifiers.update(get_identifiers(expr))
    result = []
    for loop in loops:
        start, end = loop.scope
        if start <= line_no <= end and loop.var in identifiers:
            result.append(loop.var)
    return result


def split_lhs(lhs: str) -> tuple[str, list[str]]:
    lhs = lhs.strip()
    m = re.fullmatch(r"([A-Za-z_]\w*)((?:\[[^\]]+\])*)", lhs)
    if not m:
        raise ValueError(f"unsupported lhs: {lhs}")
    return m.group(1), _BRACKET.findall(m.group(2))


def normalize_lhs(lhs: str, decl: Declaration) -> dict:
    base, brackets = split_lhs(lhs)
    if base != decl.name:
        raise ValueError(f"lhs base {base!r} does not match declaration {decl.name!r}")
    n_unpacked = len(decl.unpacked)
    if len(brackets) < n_unpacked:
        raise ValueError(f"{base} expects {n_unpacked} unpacked indices, got {len(brackets)}")
    array_indices = brackets[:n_unpacked]
    packed_selects = brackets[n_unpacked:]
    if len(packed_selects) > 1:
        raise ValueError("training scaffold supports at most one packed select after array indices")
    return {
        "base": base,
        "array_indices": array_indices,
        "slice": packed_selects[0] if packed_selects else None,
        "target": base + "".join(f"[{x}]" for x in array_indices),
    }


def parse_const_range(expr: str) -> tuple[int, int]:
    expr = expr.strip()
    if ":" not in expr:
        v = int(expr, 0)
        return v, v
    a, b = (x.strip() for x in expr.split(":", 1))
    return int(a, 0), int(b, 0)


def expand_range(msb: int, lsb: int) -> set[int]:
    lo, hi = sorted((msb, lsb))
    return set(range(lo, hi + 1))


def merge_assignments(assignments: list[dict], width: int) -> dict:
    covered: set[int] = set()
    overlap_bits: set[int] = set()
    for item in assignments:
        bits = expand_range(*item["range"])
        overlap_bits |= covered & bits
        covered |= bits
    expected = set(range(width))
    return {
        "covered_bits": covered,
        "missing_bits": expected - covered,
        "overlap_bits": overlap_bits,
        "complete": covered == expected,
        "overlap": bool(overlap_bits),
    }


def parse_auto_template(text: str) -> dict:
    """Parse a compact /* module AUTO_TEMPLATE ( ... ); */ block."""
    m = re.search(r"/\*\s*([A-Za-z_]\w*)\s+AUTO_TEMPLATE\s*\((.*?)\);\s*\*/", text, re.S)
    if not m:
        raise ValueError("AUTO_TEMPLATE block not found")
    module, body = m.groups()
    ports: dict[str, str] = {}

    i = 0
    while i < len(body):
        pm = re.search(r"\.([A-Za-z_]\w*)\s*\(", body[i:])
        if not pm:
            break
        port = pm.group(1)
        expr_start = i + pm.end()
        depth = 1
        j = expr_start
        while j < len(body) and depth:
            if body[j] == "(":
                depth += 1
            elif body[j] == ")":
                depth -= 1
            j += 1
        if depth:
            raise ValueError(f"unbalanced expression for port {port}")
        ports[port] = body[expr_start : j - 1].strip()
        i = j
    return {"module": module, "ports": ports}


def get_instance_index(instance_name: str) -> int | None:
    m = re.search(r"_(\d+)$", instance_name)
    return int(m.group(1)) if m else None


def expand_at(expr: str, index: int) -> str:
    return expr.replace("@", str(index))


def expand_template(instance_name: str, ports: dict[str, str]) -> dict[str, str]:
    index = get_instance_index(instance_name)
    if index is None:
        raise ValueError(f"cannot derive index from {instance_name}")
    return {port: expand_at(expr, index) for port, expr in ports.items()}


def validate_array_arity(expr: str, decl: Declaration) -> None:
    base, brackets = split_lhs(expr)
    if base != decl.name:
        raise ValueError(f"expression base {base} != declaration {decl.name}")
    if len(brackets) < len(decl.unpacked):
        raise ValueError(f"{base} expects {len(decl.unpacked)} unpacked indices, got {len(brackets)}")
    if len(brackets) > len(decl.unpacked) + 1:
        raise ValueError(
            f"{base} has {len(decl.unpacked)} unpacked dimensions, but expression supplies too many indices"
        )
