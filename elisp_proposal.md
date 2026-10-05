# AUTO_LISP Elisp Tests: Proposal for Trunk

## Background

7 verilog-mode official tests use `/*AUTO_LISP(defun ...)*/` to define custom
elisp functions for template expansion:

- autoinst_for_myers.v
- autoinst_func.v
- autoinst_lopaz.v
- autoinst_param_value.v
- autoinst_rogoff.v
- autoinst_tieoff_vec.v
- autolisp_order_bug356.v

## User Decision (2026-10-05)

The user explicitly declined implementing a full elisp `defun`/`setq`
interpreter ("没必要做这个"), choosing instead the Python-native
`AUTO_PYTHON` approach, which is already implemented and merged.

## Options

### Option A: Permanent xfail
Mark the 7 tests as permanent xfails with a clear reason:
"Requires elisp AUTO_LISP interpreter; user chose AUTO_PYTHON instead."

Pros:
- Zero additional work
- Honest about the scope decision

Cons:
- 7 xfails remain forever

### Option B: AUTO_PYTHON equivalents
Create new test files that exercise the same template logic via
`/*AUTO_PYTHON(...)*/` instead of `/*AUTO_LISP(defun ...)*/`.

Pros:
- Validates AUTO_PYTHON against real-world use cases
- Reduces xfail count

Cons:
- Requires translating elisp to Python for each test
- The official tests would still be xfail (we'd have parallel tests)

### Option C: Hybrid
Keep the 7 as permanent xfails, but add 2-3 AUTO_PYTHON tests that cover
the most common patterns (e.g., autoinst_func.v's bit-slicing).

## Recommendation

Option A (permanent xfail) with clear documentation. The user's decision
was explicit, and AUTO_PYTHON is already validated by its own test suite.
Translating the elisp tests would be busywork without clear benefit.

If trunk disagrees, Option C is a reasonable compromise.
