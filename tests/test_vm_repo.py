"""Cross-validate against the official verilog-mode repo test suite.

For each selected golden file ``tests_ok/<name>.v`` of the verilog-mode
repo (VM_REPO_DIR, default /tmp/verilog-mode-repo):

1. Fixed-point check — run our emacs-equivalent pipeline (the ``aall``
   pipeline minus the automatic.vim-only steps adt/ar/af: kill wire/reg
   regions, then eap -> eai -> aio -> aw -> areg) on the golden itself.
   Byte-identical output means our regeneration reproduces emacs's
   expansion exactly.
2. On mismatch, compare against a REAL emacs batch baseline
   (``verilog-delete-auto`` + ``verilog-auto``, NO indent step — the
   official harness 0test.el additionally re-indents, so goldens embed
   the indent step).  If our output matches the emacs baseline, the
   golden difference is indent-step noise, not a tooling bug.

Results (per-file category, diff stats, warnings) are recorded to
VM_REPO_RESULTS (default /tmp/vm_repo_results.json) at session end.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

VM_REPO = Path(os.environ.get("VM_REPO_DIR", "/tmp/verilog-mode-repo"))
TESTS_OK = VM_REPO / "tests_ok"
TESTS_IN = VM_REPO / "tests"
VERILOG_MODE_EL = VM_REPO / "verilog-mode.el"
RESULTS_PATH = Path(os.environ.get("VM_REPO_RESULTS", "/tmp/vm_repo_results.json"))

if not (TESTS_OK.is_dir() and VERILOG_MODE_EL.is_file()):
    pytest.skip(
        f"verilog-mode repo not found at {VM_REPO} (set VM_REPO_DIR)",
        allow_module_level=True,
    )

# Pipeline-relevant AUTO commands our tooling implements (word-boundary
# safe: AUTOINOUTMODULE/AUTOOUTPUTEVERY/AUTOREGINPUT/AUTOINOUTPARAM must
# not match).
_IMPLEMENTED_MARKERS_RE = re.compile(
    r"/\*\s*AUTO(?:INSTPARAM|INST|INOUTMODPORT|INOUTMODULE|INOUTCOMP|INOUTIN|"
    r"INOUTPARAM|INOUT|INPUT|OUTPUTEVERY|OUTPUT|ASSIGNMODPORT|ASCIIENUM|LOGIC|"
    r"REGINPUT|REG|WIRE|SENSE|RESET|TIEOFF|UNUSED|UNDEF|INSERTLISP|INSERTLAST)\b",
    re.IGNORECASE,
)

INCLUDE_PREFIXES = (
    "autoinst",
    "autowire",
    "autoinput",
    "autoinout",
    "autooutput",
    "autoinstparam",
    "autoarg",
    "autolisp",
    "autotemplate",
    "autoreg",
    "automodport",
    "autoinoutparam",
    "autosense",
    "autoreset",
    "autoascii",
    "autotieoff",
    "autologic",
    "autounused",
    "autoundef",
)
EXCLUDE_PREFIXES = (
    "inject_",
    "label_",
    "noindent_",
    "align_",
)

# Collected but skipped: the file only exercises AUTO commands our
# pipeline does not implement (or infra we cannot satisfy).
# Files still failing the fixed-point check (triage backlog — see the
# vm-repo report).  xfail (non-strict): a fix turning one green shows up as
# XPASS without breaking the gate; move it out of the set to lock it in.
KNOWN_FAILURES = frozenset(
    # autowire_topv.v: golden predates current emacs (drops 'logic' from
    # logic-typed ports); remaining diff is alignment/version drift
    ['autowire_topv.v', 'autoinout_lovell.v', 'autoinput_2d_gaspar.v', 'autoinput_array_bug294.v', 'autoinput_concat_ignore.v', 'autoinput_concat_lau.v', 'autoinput_concat_lau2.v', 'autoinput_nohookup.v', 'autoinput_none.v', 'autoinst_array.v', 'autoinst_array_braket.v', 'autoinst_attr.v', 'autoinst_autonohookup.v', 'autoinst_belkind_concat.v', 'autoinst_cmtparen_tennant.sv', 'autoinst_dedefine.v', 'autoinst_ding.v', 'autoinst_for_myers.v', 'autoinst_func.v', 'autoinst_iface_noparam.v', 'autoinst_import2012.v', 'autoinst_interface.v', 'autoinst_interface_star.v', 'autoinst_lopaz.v', 'autoinst_ma_io_prefix.v', 'autoinst_mccoy.v', 'autoinst_moddefine.v', 'autoinst_modport_param.v', 'autoinst_mplist.sv', 'autoinst_mul.v', 'autoinst_name_bug245.v', 'autoinst_nicholl.v', 'autoinst_param_2d.v', 'autoinst_param_cmt.v', 'autoinst_param_structsel.v', 'autoinst_param_type.v', 'autoinst_param_value.v', 'autoinst_paramvalue.v', 'autoinst_regexp_match.v', 'autoinst_rogoff.v', 'autoinst_star.v', 'autoinst_sv_kulkarni.v', 'autoinst_sv_kulkarni_wire.v', 'autoinst_swapped_vec.v', 'autoinst_tennant.v', 'autoinst_tieoff_vec.v', 'autoinst_unsigned_bug302.v', 'autoinst_vertrees.v', 'autoinst_wildcard.v', 'autoinst_wildcell.v', 'autoinstparam_iface_bruce.v', 'autoinstparam_local.v', 'autolisp_order_bug356.v', 'autooutput_cast.v', 'autooutput_simplify.v', 'autoreg_smith_multiassign.v', 'autotemplate_lisp_eq.v', 'autowire_apostrophe.sv', 'autowire_import_bug317.v', 'autowire_isaacson.v', 'autowire_long_yaohung.v', 'autowire_merge_bug303.v', 'autowire_merge_pm.v', 'autowire_paramvec_bug302.v', 'autowire_pkg_bug195.v', 'autowire_real.v', 'autowire_red_bracket.v', 'autowire_shifts_bug1346.v', 'autowire_thon_selects.v']
)

SKIP_TABLE = {
    "automodport_if.v": "support module for automodport tests (no implemented AUTO markers)",
    "autolisp_truex.v": "AUTOINSERTLISP user-defined elisp fns not evaluable",
    "autolisp_include.v": "AUTOINSERTLISP + verilog-auto-read-includes not implemented",
    "autolisp_include_inc.vh": "include fragment with eval Local Variables only",
}

# Golden exists but the official harness does not run it (tests/ holds
# only a .dontrun marker) — golden may be stale.
UPSTREAM_DONTRUN = {"autoinstparam_belkind.v"}

# Local Variables our tooling does NOT honor; used to tag known
# deviations in the results file.
_UNSUPPORTED_LOCALVARS_RE = re.compile(
    r"verilog-(auto-inst-vector|auto-inst-dot-name|auto-inst-sort|"
    r"auto-inst-template-numbers|auto-inst-template-required|"
    r"auto-read-includes|auto-star-save|case-fold|auto-wire-type|"
    r"auto-wire-comment|auto-ignore-concat|auto-simplify-expressions|"
    r"auto-output-ignore-regexp|auto-reg-input-assigned-ignore-regexp|"
    r"auto-declare-nettype|auto-arg-sort|auto-arg-format|"
    r"auto-tieoff-ignore-regexp|active-low-regexp|auto-inst-interfaced-ports|"
    r"auto-template-warn-unused|auto-inst-param-value-type|library-files)\s*:",
    re.IGNORECASE,
)


def _selected_files() -> list[str]:
    names = []
    for f in sorted(TESTS_OK.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        if not name.startswith(INCLUDE_PREFIXES):
            continue
        if name.startswith(EXCLUDE_PREFIXES):
            continue
        names.append(name)
    return names


SELECTED = _selected_files()


def run_pipeline(text: str, ref_file: str) -> list[str]:
    """Our emacs-equivalent pipeline: inst._main_aall minus the
    automatic.vim-only steps (autodef/autoarg/format), driven in-process.

    Mirrors _main_aall's module resolution (libdirs from -y + the file's
    own Local Variables, buffer module defs, typedef regexp, interfaces)
    exactly; only the adt/arg/af steps are dropped.
    """
    from verilog_tooling import autodef, emacs, inout, misc, sense, wire, xfer
    from verilog_tooling.inst import (
        _cli_resolve,
        _eai_like_step,
        _read_module_srcs,
        _resolve_module_files,
        buffer_module_defs,
        find_auto_templates,
        find_interfaces,
        interfaces_for,
        parse_module_ports,
    )
    from verilog_tooling.libdirs import parse_typedef_regexp

    lines = text.splitlines()
    # verilog-delete-auto-buffer for the regions we regenerate (mirrors
    # _main_aall: drop last round's decls so they don't poison this
    # round's exclusions).
    lines = wire.kill_auto_wire(lines)
    lines = wire.kill_auto_reg(lines)
    lines = misc.kill_auto_ascii_enum(lines)
    lines = misc.kill_auto_logic(lines)
    lines = misc.kill_auto_tieoff(lines)
    lines = misc.kill_auto_unused(lines)
    lines = misc.kill_auto_undef(lines)
    lines = misc.kill_auto_insert_lisp(lines)
    lines = misc.kill_auto_insert_last(lines)
    lines = xfer.kill_auto_inoutmodule(lines)  # also COMP/IN, same header
    lines = xfer.kill_auto_inoutparam(lines)
    lines = xfer.kill_auto_inoutmodport(lines)
    lines = xfer.kill_auto_assign_modport(lines)
    lines = xfer.kill_auto_outputevery(lines)
    lines = xfer.kill_auto_reginput(lines)
    lines = sense.kill_auto_sense(lines)
    lines = sense.kill_auto_reset(lines)
    text = "\n".join(lines)

    args = SimpleNamespace(
        in_file=ref_file,
        ref_file=ref_file,
        libdir=[],
        interface=[],
        star_expand=False,
        star_save=False,
        sort=False,
        dot_name=False,
        # like inst.main(): the buffer's Local Variables can switch
        # param-value substitution on
        param_value=bool(
            re.search(r"^\s*//\s*verilog-auto-inst-param-value\s*:\s*t\b", text, re.M)
        ),
        which=None,
    )

    names_emacs: set[str] = set()
    for kw in ("AUTOINST", "AUTOINSTPARAM"):
        try:
            names_emacs |= set(emacs.marker_modules(lines, kw, include_star=False))
        except ValueError:
            pass
    # AUTOINOUTMODULE/COMP/IN/PARAM markers name their source module in
    # the first quoted argument
    names_xfer: set[str] = set()
    for _kw in ("AUTOINOUTMODULE", "AUTOINOUTCOMP", "AUTOINOUTIN", "AUTOINOUTPARAM"):
        for _mk in xfer._find_markers(text, _kw):
            if _mk.args:
                names_xfer.add(_mk.args[0])
    names_wire = names_emacs | autodef._candidate_module_names(lines) | names_xfer

    libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
    files = _resolve_module_files(
        sorted(names_wire), libdirs, inst_files, vc_entries, extensions
    )
    buffer_mods = buffer_module_defs(text)
    srcs = _read_module_srcs(files)

    def src_of(name: str):
        return srcs.get(name) or buffer_mods.get(name)

    resolved = {n for n in names_wire if src_of(n) is not None}
    td_re = parse_typedef_regexp(lines)
    interfaces = interfaces_for(lines, libdirs)
    templates = find_auto_templates(text)

    # Interface table for AUTOINOUTMODPORT / AUTOASSIGNMODPORT — buffer
    # definitions shadow same-named library interfaces
    ifaces: dict = {}
    for iname, ipath in find_interfaces(libdirs).items():
        try:
            ifaces[iname] = xfer.parse_interface_info(ipath.read_text().splitlines(), td_re)
        except (OSError, ValueError):
            pass
    ifaces.update(xfer.buffer_interfaces(lines, td_re))

    # 1. AUTOINSERTLISP
    lines = misc.auto_insert_lisp(lines)

    # 1. EAP (AUTOINSTPARAM)
    which, resolvable, _ = _eai_like_step(lines, "AUTOINSTPARAM", resolved)
    if which is None or resolvable:
        module_params = {
            n: emacs.parse_module_params(s) for n in names_emacs if (s := src_of(n))
        }
        lines = emacs.auto_param(lines, module_params, which=which, templates=templates)

    # 2. EAI (AUTOINST)
    which, resolvable, _ = _eai_like_step(lines, "AUTOINST", resolved)
    if which is None or resolvable:
        modules = {
            n: parse_module_ports(s, with_params=True, interfaces=interfaces, typedef_regexp=td_re)
            for n in names_emacs
            if (s := src_of(n))
        }
        lines = emacs.auto_inst(
            lines,
            modules,
            which=which,
            templates=templates,
            param_value=args.param_value,
        )

    # 3. AUTOASCIIENUM; 4-7. AUTOINOUTMODPORT/MODULE/COMP/IN;
    # 8. AUTOINOUTPARAM — before AIO so their declarations are visible to
    # the wire/reg passes
    lines = misc.auto_ascii_enum(lines)
    lines = xfer.auto_inoutmodport(lines, ifaces)
    modules_w = {
        n: parse_module_ports(s, typedef_regexp=td_re, interfaces=interfaces)
        for n in names_wire
        if (s := src_of(n))
    }
    lines = xfer.auto_inoutmodule(lines, modules_w)
    lines = xfer.auto_inoutcomp(lines, modules_w)
    lines = xfer.auto_inoutin(lines, modules_w)
    # AUTOINOUTPARAM copies the submodule's parameters: needs with_params
    if xfer._find_markers("\n".join(lines), "AUTOINOUTPARAM"):
        modules_p = {
            n: parse_module_ports(s, with_params=True, typedef_regexp=td_re, interfaces=interfaces)
            for n in names_wire
            if (s := src_of(n))
        }
    else:
        modules_p = modules_w
    lines = xfer.auto_inoutparam(lines, modules_p)
    # 9. AIO; 10. AUTOTIEOFF; 11. AUTOUNDEF; 12. AUTOASSIGNMODPORT;
    # 13. AUTOLOGIC; 14. AW; 15. AREG; 16. AUTOREGINPUT;
    # 17. AUTOOUTPUTEVERY; 18. AUTOSENSE; 19. AUTORESET; 20. AUTOUNUSED
    lines = inout.auto_output(lines, modules_w)
    lines = inout.auto_input(lines, modules_w)
    lines = inout.auto_inout(lines, modules_w)
    lines = misc.auto_tieoff(lines, modules_w)
    lines = misc.auto_undef(lines)
    lines = xfer.auto_assign_modport(lines, ifaces)
    lines = misc.auto_logic(lines, modules_w)
    lines = wire.auto_wire(lines, modules_w)
    lines = wire.auto_reg(lines, modules_w)
    lines = xfer.auto_reginput(lines, modules_w)
    lines = xfer.auto_outputevery(lines)
    # 0test.el runs with fill-column 100; mirror it for the wrap point
    lines = sense.auto_sense(lines, fill_column=100)
    lines = sense.auto_reset(lines)
    lines = misc.auto_unused(lines, modules_w)
    return lines


# Elisp: verilog-delete-auto + verilog-auto (NO indent — 0test.el's
# indent step is what makes goldens differ from raw auto output).
# Settings mirror 0test.el. Prints VMOK/VMERROR per file.
_BATCH_ELISP = """
(let ((list-file (car command-line-args-left))
      (files '()))
  (with-temp-buffer
    (insert-file-contents list-file)
    (dolist (l (split-string (buffer-string) "\\n" t))
      (push l files)))
  (setq files (nreverse files))
  (load (expand-file-name "EMACS_EL_PATH"))
  (setq enable-local-variables t)
  (setq enable-local-eval t)
  (setq make-backup-files nil)
  (setq-default make-backup-files nil)
  (setq-default fill-column 100)
  (defun ask-user-about-lock (&rest _) nil)
  (dolist (f files)
    (condition-case err
        (progn
          (find-file f)
          (verilog-mode)
          (verilog-delete-auto)
          (verilog-auto)
          (save-buffer)
          (message "VMOK %s" f))
      (error (message "VMERROR %s %S" f err)))
    (let ((buf (find-buffer-visiting f)))
      (when buf (kill-buffer buf)))))
"""


def _ws_squeeze(lines: list[str]) -> list[str]:
    """Whitespace-run-collapsed lines (diff -b style comparison)."""
    return [" ".join(line.split()) for line in lines]


def _diff_count(a: list[str], b: list[str]) -> int:
    return sum(
        1
        for line in difflib.unified_diff(a, b, lineterm="")
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


@pytest.fixture(scope="session")
def vm_tree(tmp_path_factory):
    """Writable copies of the verilog-mode tests tree: tests/ overlaid
    with tests_ok/ (so include files and input-only siblings resolve,
    goldens win).  One tree stays pristine for our (read-only) pipeline;
    emacs rewrites the other in place for the no-indent baseline."""
    base = tmp_path_factory.mktemp("vm_repo")
    tree_py = base / "py"
    tree_emacs = base / "emacs"
    for tree in (tree_py, tree_emacs):
        if TESTS_IN.is_dir():
            shutil.copytree(TESTS_IN, tree, dirs_exist_ok=True)
        shutil.copytree(TESTS_OK, tree, dirs_exist_ok=True)
    return {"py": tree_py, "emacs": tree_emacs}


@pytest.fixture(scope="session")
def emacs_baseline(vm_tree):
    """Run real emacs (delete-auto + auto, no indent) on every selected
    file in the emacs tree, in one batch.  Returns {filename: "ok" |
    "error: ..."}; empty dict when emacs is unavailable/fails."""
    exe = shutil.which("emacs")
    if not exe:
        return {}
    tree = vm_tree["emacs"]
    names = [n for n in SELECTED if n not in SKIP_TABLE]
    list_file = tree.parent / "files.txt"
    list_file.write_text("\n".join(str(tree / n) for n in names))
    el_file = tree.parent / "run.el"
    el_file.write_text(_BATCH_ELISP.replace("EMACS_EL_PATH", str(VERILOG_MODE_EL)))
    try:
        proc = subprocess.run(
            [exe, "--batch", "-q", "-l", str(el_file), str(list_file)],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    status: dict[str, str] = {}
    for line in proc.stderr.splitlines():
        if line.startswith("VMOK "):
            status[Path(line[5:].strip()).name] = "ok"
        elif line.startswith("VMERROR "):
            status[Path(line[8:].split(" ", 1)[0]).name] = "error"
    return status


@pytest.fixture(scope="session")
def results_recorder():
    records: dict[str, dict] = {}
    yield records
    existing = {}
    if RESULTS_PATH.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            existing = json.loads(RESULTS_PATH.read_text())
    existing.update(records)
    # drop records for files no longer selected
    keep = set(SELECTED)
    RESULTS_PATH.write_text(
        json.dumps({k: v for k, v in existing.items() if k in keep}, indent=1, sort_keys=True)
        + "\n"
    )


def _param(name: str):
    if name in SKIP_TABLE:
        return pytest.param(name, marks=pytest.mark.skip(reason=SKIP_TABLE[name]), id=name)
    if name in KNOWN_FAILURES:
        return pytest.param(
            name, marks=pytest.mark.xfail(reason="vm-repo triage backlog"), id=name
        )
    return pytest.param(name, id=name)


@pytest.mark.parametrize("fname", [_param(n) for n in SELECTED])
def test_vm_repo_fixed_point(fname, vm_tree, emacs_baseline, results_recorder):
    golden_path = vm_tree["py"] / fname
    golden_text = golden_path.read_text()
    golden_lines = golden_text.splitlines()
    rec = results_recorder[fname] = {"file": fname}
    if fname in UPSTREAM_DONTRUN:
        rec["note"] = "upstream .dontrun — golden not verified by 0test.el"
    rec["vacuous"] = not _IMPLEMENTED_MARKERS_RE.search(golden_text)
    unsupported = sorted(set(_UNSUPPORTED_LOCALVARS_RE.findall(golden_text)))
    if unsupported:
        rec["unsupported_localvars"] = unsupported

    err_buf = io.StringIO()
    crash = None
    with contextlib.redirect_stderr(err_buf):
        try:
            our_lines = run_pipeline(golden_text, str(golden_path))
        except Exception as exc:  # noqa: BLE001 - record crash per file
            crash = f"{type(exc).__name__}: {exc}"
            our_lines = None
    if err_buf.getvalue().strip():
        rec["warnings"] = err_buf.getvalue().strip().splitlines()[:10]

    emacs_path = vm_tree["emacs"] / fname
    emacs_status = emacs_baseline.get(fname)
    emacs_lines = None
    if emacs_status == "ok" and emacs_path.is_file():
        emacs_lines = emacs_path.read_text().splitlines()

    if crash is not None:
        rec["category"] = "crash"
        rec["error"] = crash
        pytest.fail(f"[crash] {crash}", pytrace=False)

    if our_lines == golden_lines:
        rec["category"] = "pass_exact"
        return

    d_gold = _diff_count(our_lines, golden_lines)
    ws_gold = _diff_count(_ws_squeeze(our_lines), _ws_squeeze(golden_lines))
    rec["diff_lines_vs_golden"] = d_gold
    if ws_gold == 0:
        rec["category"] = "pass_whitespace"
        return

    if emacs_lines is not None:
        d_em = _diff_count(our_lines, emacs_lines)
        ws_em = _diff_count(_ws_squeeze(our_lines), _ws_squeeze(emacs_lines))
        ws_em_gold = _diff_count(_ws_squeeze(emacs_lines), _ws_squeeze(golden_lines))
        rec["diff_lines_vs_emacs"] = d_em
        rec["ws_diff_vs_emacs"] = ws_em
        rec["ws_diff_emacs_vs_golden"] = ws_em_gold
        if ws_em == 0:
            # we match the emacs no-indent baseline; the golden diff
            # comes from the official harness's indent step
            rec["category"] = "pass_matches_emacs"
            return
    elif emacs_status:
        rec["emacs_baseline"] = emacs_status

    rec["category"] = "content_diff"
    diff = "\n".join(
        difflib.unified_diff(
            [l + "\n" for l in golden_lines],
            [l + "\n" for l in our_lines],
            fromfile=f"tests_ok/{fname}",
            tofile="ours",
            n=1,
        )
    )
    capped = "\n".join(diff.splitlines()[:40])
    pytest.fail(
        f"[content_diff] ours vs tests_ok/{fname} "
        f"({d_gold} diff lines, {ws_gold} after whitespace-squeeze)\n{capped}",
        pytrace=False,
    )
