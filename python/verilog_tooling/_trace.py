"""VERILOG_TOOLING_TRACE signal-dimension tracing (debug observability).

Set ``VERILOG_TOOLING_TRACE`` to a comma-separated list of signal names
(case-sensitive; ``*`` traces everything) and the AUTOWIRE/AUTODEF
dimension decisions for those signals are printed to stderr as

    [vt-trace] <sig>: <EVENT> key=value ...

Events use fixed verbs (first-driven / conn-dims / skip-declared /
emit-decl / extend-from-side / merge-dims / update-width / update-dims /
dt-exempt / absorb / waive-keep / multi-driver) so the output greps well.
Unset/empty variable: one cached os.environ read, no regex on hot paths.
The trace is a debugging switch: it is NOT suppressed by
VERILOG_TOOLING_QUIET.  Pure observation — tracing never changes behavior.
"""

from __future__ import annotations

import os
import sys

# None = env not read yet; False = off; True = '*'; frozenset = names
_STATE = None


def _state():
    global _STATE
    if _STATE is None:
        raw = os.environ.get("VERILOG_TOOLING_TRACE", "")
        names = frozenset(n.strip() for n in raw.split(",") if n.strip())
        if "*" in names:
            _STATE = True
        elif names:
            _STATE = names
        else:
            _STATE = False
    return _STATE


def tracing(sig: str) -> bool:
    """Whether SIG is being traced (guard for event-string construction)."""
    st = _state()
    return st is True or (st is not False and sig in st)


def trace_sig(sig: str, event: str) -> None:
    """Emit one trace line for SIG when it is traced."""
    if tracing(sig):
        print(f"[vt-trace] {sig}: {event}", file=sys.stderr)


def _reset_for_tests() -> None:
    """Drop the cached env read (tests set the variable per-case)."""
    global _STATE
    _STATE = None
