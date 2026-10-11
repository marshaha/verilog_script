# Differential Testing Methodology

User-proposed methodology for finding bugs in verilog-mode auto features
by comparing tool output against hand-written real RTL.

## Methods

### 1. AUTOINST Differential (`autoinst_diff.py`)

**Idea**: Hand-written instance → `/*AUTOINST*/` template → EAI expand → compare port lists.

```bash
python3 autoinst_diff.py <projdir> <libdir>
```

If EAI generates different ports than hand-written, it's a bug (or the
hand-written was non-standard).

**Validates**: EAI port expansion correctness on real RTL.

### 2. AUTODEF Conflict Injection (`autodef_conflict.py`)

**Idea**: Insert `/*autodef*/` after module header → run autodef → check
`//AD_CONFLICT` notes. A wrong inference proves a script bug.

```bash
python3 autodef_conflict.py <projdir> <libdir>
```

**Validates**: autodef width inference on real RTL.

## Project Registry

`projects_registry.json` accumulates tested open-source RTL projects for
regression testing.

## Bugs Found

This methodology found 5 real bugs (2026-10-11):
1. Dotted LHS (`hw2reg.val.d` → bogus `d` signal)
2. Concat LHS width smear (`{1b,32b}=33b`)
3. Param paren handling
4. Import-clause param parsing (`module ... import pkg::*; #(...)`)
5. `_instance_headers` missing `) u_reg (` same-line param instances
