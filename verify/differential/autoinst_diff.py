#!/usr/bin/env python3
"""
Differential testing for verilog-mode auto features (user's idea).

1. Take real RTL with hand-written instances
2. Replace instance body with /*AUTOINST*/ template
3. Run auto_inst via Python API
4. Diff port lists vs original hand-written
5. Differences = potential bugs
"""
import re, sys
sys.path.insert(0, "/home/hatch/workspace/gh_new/vs-work/python")
from pathlib import Path
from verilog_tooling.emacs import auto_inst
from verilog_tooling.inst import parse_module_ports

def load_modules(libdir):
    """Load all .sv modules from libdir into dict."""
    from verilog_tooling.inst import find_interfaces
    # Discover interfaces first
    try:
        iface_files = find_interfaces([libdir])
        interfaces = set(iface_files.keys())
    except:
        interfaces = set()
    mods = {}
    for f in Path(libdir).rglob("*.sv"):
        try:
            src = f.read_text(errors="replace").splitlines()
            mdef = parse_module_ports(src, interfaces=interfaces)
            if mdef.name:
                mods[mdef.name] = mdef
        except:
            pass
    return mods

def extract_first_instance(txt):
    keywords = {'module','input','output','inout','wire','reg','logic',
                'assign','always','initial','for','if','begin','end',
                'parameter','localparam','genvar','integer'}
    for m in re.finditer(r"^\s*(\w+)\s*(?:#\s*\([^;]*?\)\s*)?(\w+)\s*\(", txt, re.M):
        mod, inst = m.group(1), m.group(2)
        if mod in keywords:
            continue
        start = m.start()
        depth = 0
        i = m.end() - 1
        while i < len(txt):
            c = txt[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    j = i + 1
                    while j < len(txt) and txt[j] in ' \t\n':
                        j += 1
                    if j < len(txt) and txt[j] == ';':
                        return (mod, inst, start, j+1, txt[start:j+1])
                    break
            i += 1
    return None

def main():
    proj = Path(sys.argv[1])
    lib = sys.argv[2] if len(sys.argv) > 2 else str(proj)
    print(f"Loading modules from {lib}...")
    mods = load_modules(lib)
    print(f"Loaded {len(mods)} modules")
    
    count = 0
    diffs = 0
    for f in proj.rglob("*.sv"):
        if count >= 15:
            break
        try:
            txt = f.read_text(errors="replace")
        except:
            continue
        inst_info = extract_first_instance(txt)
        if not inst_info:
            continue
        mod, inst, s, e, full = inst_info
        if '.' in mod:  # interface instance, skip
            continue
        if mod not in mods:
            continue
        if full.count('.') < 5:
            continue
        count += 1
        
        # Make template: preserve #() params, only replace port list
        # Find the port list paren (after #() if present, else first paren)
        if '#(' in full:
            # Find ) of #(), then ( of ports
            pm = re.search(r"#\s*\([^;]*?\)\s*(\w+\s*)?\(", full)
            if not pm:
                continue
            paren_start = pm.end() - 1
        else:
            m = re.search(r"\(\s*", full)
            if not m:
                continue
            paren_start = m.start()
        header_part = full[:paren_start+1]
        template_txt = txt[:s] + header_part + "\n  /*AUTOINST*/\n);" + txt[e:]
        template_lines = template_txt.splitlines()
        
        # Run auto_inst
        try:
            out_lines = auto_inst(template_lines, mods)
        except Exception as ex:
            print(f"  {f.name}: FAIL - {ex}")
            continue
        
        out_txt = "\n".join(out_lines)
        # Extract ports from output
        out_inst = extract_first_instance(out_txt)
        if not out_inst:
            print(f"  {f.name}: FAIL - no instance in output")
            continue
        _, _, _, _, new_full = out_inst
        # Strip comments before extracting (avoid // SEC_CM: BUS.INTEGRITY)
        full_nc = re.sub(r"//.*", "", full)
        new_nc = re.sub(r"//.*", "", new_full)
        # Skip interface connections (e.g., .apb4(cfg_apb4) where apb4 is interface)
        # These are known limitation in parse_module_ports
        orig_ports = sorted(re.findall(r"(?<![\w])\.(\w+)\s*[(,)]", full_nc))
        new_ports = sorted(re.findall(r"(?<![\w])\.(\w+)\s*[(,)]", new_nc))
        # Filter out known interface ports (heuristic: if not in module def)
        # For now, just note that interface ports may differ
        if orig_ports == new_ports:
            print(f"  {f.name}: OK ({len(orig_ports)} ports)")
        else:
            diffs += 1
            missing = sorted(set(orig_ports) - set(new_ports))
            extra = sorted(set(new_ports) - set(orig_ports))
            print(f"  {f.name}: DIFF missing={missing[:3]} extra={extra[:3]}")
    
    print(f"\n{count} tested, {diffs} diffs")

def load_vc(vc_path):
    """Load -y dirs from .vc file."""
    dirs = []
    with open(vc_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("-y "):
                dirs.append(line[3:].strip())
    return dirs

if __name__ == "__main__":
    # Support: python3 diff_sweep.py <proj> <vc_file|--vc>
    import sys
    if len(sys.argv) > 2 and sys.argv[2].endswith(".vc"):
        vc_dirs = load_vc(sys.argv[2])
        # Replace lib arg with vc dirs
        sys.argv[2] = ",".join(vc_dirs)
    main()
