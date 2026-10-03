"""verilog-mode ``verilog-auto-template-lint`` port.

Checks ``/*... AUTO_TEMPLATE (...); */`` blocks for lines never consumed by
any AUTOINST/AUTOINSTPARAM expansion, mirroring
``verilog-auto-template-warn-unused``: during the EAI/EAP expansion every
template entry actually used to connect a pin is recorded (via the
``on_hit`` hook on :func:`verilog_tooling.template.template_connection`);
entries with zero hits are reported as::

    <file>:<line>: AUTO_TEMPLATE line unused: ".<pattern> (<connection>)"

Only templates whose module actually has instances in the buffer are
considered (a template for an uninstantiated module is not "unused",
it is simply inapplicable — verilog-mode only warns for templates it
looked at).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def lint_templates(
    lines: Sequence[str],
    modules: dict,
    ref_name: str = "<buffer>",
) -> list[str]:
    """Return warning strings for unused AUTO_TEMPLATE entries."""
    from . import emacs
    from .template import TemplateEntry, find_auto_templates, template_connection

    text = "\n".join(lines)
    templates = find_auto_templates(text)
    if not templates:
        return []

    hits: set[tuple[int, int]] = set()

    # wrap template_connection used by emacs.auto_inst / auto_param
    import verilog_tooling.template as _tmpl

    orig = _tmpl.template_connection

    def wrapped(template, port_name, at_value, port_width, env=None, **kw):
        def _hit(entry: TemplateEntry) -> None:
            hits.add((id(template), id(entry)))

        return orig(
            template, port_name, at_value, port_width, env, on_hit=_hit, **kw
        )

    _tmpl.template_connection = wrapped
    try:
        emacs.auto_inst(list(lines), modules, templates=templates)
        emacs.auto_param(list(lines), modules, templates=templates)
    finally:
        _tmpl.template_connection = orig

    # which templates were even applicable? (module instantiated in buffer)
    inst_mods: set[str] = set()
    try:
        for idx in emacs.find_auto_markers(lines, "AUTOINST"):
            inst_mods.add(emacs.marker_module(lines, idx))
    except Exception:
        pass
    try:
        for idx in emacs.find_auto_markers(lines, "AUTOINSTPARAM"):
            inst_mods.add(emacs.marker_module(lines, idx))
    except Exception:
        pass

    warnings = []
    for tpl in templates:
        if not (set(tpl.modules) & inst_mods):
            continue
        for e in tpl.entries:
            if (id(tpl), id(e)) not in hits:
                lineno = tpl.line_no + 1
                warnings.append(
                    f"{ref_name}:{lineno}: AUTO_TEMPLATE line unused: "
                    f'".{e.pattern} ({e.connection})"'
                )
    return warnings


def create_by_args(args_l=None):
    parser = argparse.ArgumentParser(
        prog="verilog_tooling.lint",
        description="verilog-mode verilog-auto-template-lint rewrite",
    )
    parser.add_argument(
        "command", choices=["lint"], help="lint: warn about unused AUTO_TEMPLATE lines"
    )
    parser.add_argument("-i", "--in_file", required=True)
    parser.add_argument("-o", "--out_file", default=None, help="write warnings here (default: stdout)")
    parser.add_argument("--ref_file", default=None)
    parser.add_argument("-y", "--libdir", action="append", default=[])
    parser.add_argument("-I", "--interface", action="append", default=[])
    return parser.parse_args(args_l)


def main(argv=None) -> None:
    from .inst import (
        _cli_resolve,
        _module_lines,
        _resolve_module_files,
        buffer_module_defs,
        find_interfaces,
        parse_module_ports,
    )
    from .libdirs import parse_typedef_regexp

    args = create_by_args(argv)
    lines = Path(args.in_file).read_text().splitlines()

    from . import emacs as _emacs

    names: set[str] = set()
    for kw in ("AUTOINST", "AUTOINSTPARAM"):
        try:
            names |= set(_emacs.marker_modules(lines, kw))
        except ValueError:
            pass
    modules = {}
    if names:
        libdirs, inst_files, vc_entries, extensions = _cli_resolve(args, lines)
        files = _resolve_module_files(sorted(names), libdirs, inst_files, vc_entries, extensions)
        buffer_mods = buffer_module_defs("\n".join(lines))
        td_re = parse_typedef_regexp(lines)
        interfaces = set(find_interfaces(libdirs)) | set(args.interface)
        for name in names:
            src = _module_lines(name, files, buffer_mods)
            if src is not None:
                modules[name] = parse_module_ports(
                    src, with_params=True, typedef_regexp=td_re, interfaces=interfaces
                )

    warnings = lint_templates(lines, modules, ref_name=args.ref_file or args.in_file)
    text = "\n".join(warnings) + ("\n" if warnings else "")
    if args.out_file:
        Path(args.out_file).write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
