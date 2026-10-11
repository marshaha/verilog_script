#!/usr/bin/env python3
"""
Differential testing for AUTOINPUT/AUTOOUTPUT/AUTOINOUT (user's idea).

Correct method (user corrected):
1. Original has: `input [7:0] cio_rx_i` + instance `.rx(cio_rx_i)`
2. Template: replace input decls with /*AUTOINPUT*/, KEEP instance as-is
3. Run auto_input: analyzes instance, sees cio_rx_i needed, generates it
4. Compare: name + width MUST match exactly

Input port naming is deterministic from signal names in instances.
"""
import re, sys
sys.path.insert(0, "/home/hatch/workspace/gh_new/vs-work/python")
from pathlib import Path
from verilog_tooling.inout import auto_input, auto_output, auto_inout
from verilog_tooling.inst import parse_module_ports

def load_modules(libdirs):
    mods = {}
    for libdir in libdirs:
        for f in Path(libdir).rglob("*.sv"):
            try:
                src = f.read_text(errors="replace").splitlines()
                mdef = parse_module_ports(src)
                if mdef.name and mdef.name not in mods:
                    mods[mdef.name] = mdef
            except:
                pass
    return mods

def find_module_header(txt):
    """Find ANSI module header port list."""
    m = re.search(r"module\s+(\w+)", txt)
    if not m: return None
    idx = m.end()
    while idx < len(txt):
        if txt[idx:idx+6] == "import":
            j = txt.find(";", idx)
            idx = j + 1 if j > 0 else len(txt)
        elif txt[idx] == "#":
            depth, j = 0, idx
            while j < len(txt):
                if txt[j] == '(': depth += 1
                elif txt[j] == ')':
                    depth -= 1
                    if depth == 0: break
                j += 1
            idx = j + 1
        elif txt[idx] == '(':
            depth, j = 1, idx + 1
            while j < len(txt) and depth > 0:
                if txt[j] == '(': depth += 1
                elif txt[j] == ')': depth -= 1
                j += 1
            return (m.group(1), idx+1, j-1)
        elif txt[idx] == ';':
            return None
        else:
            idx += 1
    return None

def extract_ports_with_width(portlist, direction):
    """Extract (name, width_text, is_struct) for given direction.
    is_struct=True if the port uses a package::type or custom type."""
    # Match: direction [width] [pkg::type] name
    results = []
    # Split portlist into individual port declarations
    # Handle commas inside brackets
    parts = []
    depth, cur = 0, ""
    for c in portlist:
        if c == '[': depth += 1
        elif c == ']': depth -= 1
        elif c == ',' and depth == 0:
            parts.append(cur); cur = ""
            continue
        cur += c
    if cur.strip(): parts.append(cur)
    
    for p in parts:
        p = p.strip()
        # Check if starts with direction
        m = re.match(rf"{direction}\s+(.*)", p, re.S)
        if not m: continue
        rest = m.group(1).strip()
        # Extract width [...] if present
        width = ""
        wm = re.match(r"(\[.*?\])\s*(.*)", rest, re.S)
        if wm:
            width = wm.group(1)
            rest = wm.group(2).strip()
        # Check for pkg::type or custom type
        is_struct = False
        tm = re.match(r"(\w+::\w+|\w+_t)\s+(\w+)", rest)
        if tm:
            # Has explicit type like pkg::type or foo_t
            is_struct = True
            name = tm.group(2)
        else:
            tm2 = re.match(r"(\w+)", rest)
            if tm2:
                name = tm2.group(1)
            else:
                continue
        if name not in ('input','output','inout','logic','wire','reg'):
            results.append((name, width, is_struct))
    return results

def test_feature(projdir, libdirs, feature="input", limit=15):
    mods = load_modules(libdirs)
    print(f"Loaded {len(mods)} modules")
    
    func_map = {"input": (auto_input, "/*AUTOINPUT*/", "input"),
                "output": (auto_output, "/*AUTOOUTPUT*/", "output"),
                "inout": (auto_inout, "/*AUTOINOUT*/", "inout")}
    func, marker, direction = func_map[feature]
    
    count, diffs = 0, 0
    for f in Path(projdir).rglob("*.sv"):
        if count >= limit: break
        try: txt = f.read_text(errors="replace")
        except: continue
        
        hdr = find_module_header(txt)
        if not hdr: continue
        _, ps, pe = hdr
        portlist = txt[ps:pe]
        orig = extract_ports_with_width(portlist, direction)
        if len(orig) < 3: continue
        # Convert to dict, filtering structs later
        orig_list = orig
        
        # Must have instances (for auto_input to analyze)
        if not re.search(r"^\s*\w+\s+\w+\s*\(", txt, re.M):
            continue
        count += 1
        
        # Template: surgically replace ONLY the target direction ports
        # Parse portlist, keep other directions, replace target with marker
        import re as _re
        # Split portlist, identify target-direction decls
        # For simplicity: replace the entire portlist content between ( and )
        # with: [non-target ports] + marker
        # Extract non-target ports
        nontarget = []
        # Split by commas (respecting brackets)
        parts, depth, cur = [], 0, ""
        for ch in portlist:
            if ch == '[': depth += 1
            elif ch == ']': depth -= 1
            elif ch == ',' and depth == 0:
                parts.append(cur); cur = ""; continue
            cur += ch
        if cur.strip(): parts.append(cur)
        for p in parts:
            ps_strip = p.strip()
            # Check if this part is a target-direction declaration
            # A part is target if it starts with direction OR continues a previous decl
            # (simplified: check if direction keyword appears)
            if not _re.match(rf"{direction}\b", ps_strip):
                nontarget.append(p)
        # Reconstruct: nontarget ports + marker
        new_portlist = ",".join(nontarget)
        if new_portlist.strip():
            new_portlist += ",\n"
        new_portlist += f"  {marker}\n"
        t = txt[:ps] + new_portlist + txt[pe:]
        
        try:
            out = func(t.splitlines(), mods)
        except Exception as ex:
            print(f"  {f.name}: FAIL - {ex}")
            continue
        
        out_txt = "\n".join(out)
        hdr2 = find_module_header(out_txt)
        if not hdr2:
            print(f"  {f.name}: FAIL no header")
            continue
        _, ps2, pe2 = hdr2
        new_portlist = out_txt[ps2:pe2]
        new = extract_ports_with_width(new_portlist, direction)
        
        orig_dict = {name: width for name, width, _ in orig}
        new_dict = {name: width for name, width, _ in new}
        # Filter: auto_input with simple_only skips struct-typed ports
        # (e.g., uart_reg2hw_t reg2hw). Only compare simple ports.
        # A port is "simple" if its width is a bit-range or empty, not a type.
        # For now, skip ports where orig width contains '::' or is a type name.
        # Actually: just compare the intersection of simple ports.
        # Simple heuristic: if width is empty or [..], it's simple.
        def is_simple(w):
            return w == "" or w.startswith("[")
        orig_simple = {k: v for k, v in orig_dict.items() if is_simple(v)}
        new_simple = {k: v for k, v in new_dict.items() if is_simple(v)}
        if orig_simple == new_simple:
            print(f"  {f.name}: OK ({len(orig)} {direction}s)")
        else:
            diffs += 1
            missing = {k: orig_simple[k] for k in orig_simple if k not in new_simple}
            extra = {k: new_simple[k] for k in new_simple if k not in orig_simple}
            width_diff = {k: (orig_simple[k], new_simple[k]) for k in orig_simple 
                         if k in new_simple and orig_simple[k] != new_simple[k]}
            detail = []
            if missing: detail.append(f"missing={list(missing.items())[:3]}")
            if extra: detail.append(f"extra={list(extra.items())[:3]}")
            if width_diff: detail.append(f"width={list(width_diff.items())[:3]}")
            print(f"  {f.name}: DIFF {' '.join(detail)}")
    
    print(f"\n{count} tested, {diffs} diffs")

if __name__ == "__main__":
    proj = sys.argv[1]
    libs = sys.argv[2].split(",") if len(sys.argv) > 2 else [sys.argv[1]]
    feature = sys.argv[3] if len(sys.argv) > 3 else "input"
    test_feature(proj, libs, feature)
