"""Sweep real projects for //AD_CONFLICT notes: inject /*autodef*/ into
modules with hand-written declarations + subinstances, run adt, collect notes."""
import re, subprocess, sys, os
from pathlib import Path

SRC = "/home/hatch/workspace/gh_new/vs-work/python"
env = dict(os.environ, PYTHONPATH=SRC)

def find_candidates(projdir, limit=20):
    """Modules with hand-written multidim wire/reg AND subinstances."""
    cands = []
    for f in Path(projdir).rglob("*.sv"):
        if len(cands) >= limit: break
        try: txt = f.read_text(errors="replace")
        except: continue
        # hand-written declaration with dims (single or multi)
        if not re.search(r"^\s*(wire|reg|logic)\s*(\[[^\]]+\])+\s*\w+\s*;", txt, re.M):
            continue
        # has subinstances (heuristic: module instantiation)
        if not re.search(r"^\s*\w+\s+(#\s*\(.*?\)\s*)?\w+\s*\(", txt, re.M):
            continue
        # skip testbenches
        if "tb" in f.name or "test" in f.name.lower():
            continue
        cands.append(f)
    return cands

def run_adt(topfile, libdirs):
    work = Path("/tmp/conflict_sweep") / topfile.stem
    work.mkdir(parents=True, exist_ok=True)
    src = topfile.read_text(errors="replace")
    # inject after module header
    m = re.search(r"\bmodule\s+\w+.*?\)\s*;", src, re.S)
    if not m: return None
    injp, outp = work/"inj.v", work/"out.v"
    injp.write_text(src[:m.end()] + "\n/*autodef*/\n" + src[m.end():])
    cmd = [sys.executable, "-m", "verilog_tooling.autodef", "adt",
           "-i", str(injp), "-o", str(outp)]
    for d in libdirs: cmd += ["-y", d]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if p.returncode != 0 or not outp.exists():
        return None
    out = outp.read_text(errors="replace")
    notes = re.findall(r"^\s*.*//AD_CONFLICT:\s*(.+)$", out, re.M)
    return notes

if __name__ == "__main__":
    import sys
    proj = sys.argv[1]
    lib = sys.argv[2] if len(sys.argv) > 2 else proj
    # For OpenTitan, also include prim rtl so prim_* submodules resolve
    # (otherwise port widths fall back to heuristics and cause false conflicts)
    libs = [lib]
    ot_prim = Path("/home/hatch/workspace/gh_new/opentitan_full/hw/ip/prim/rtl")
    if ot_prim.is_dir() and str(ot_prim) not in libs:
        # only add if we're sweeping inside opentitan_full
        if "opentitan_full" in str(proj):
            libs.append(str(ot_prim))
    cands = find_candidates(proj, limit=20)
    print(f"Found {len(cands)} candidates in {proj}")
    for f in cands:
        notes = run_adt(f, libs)
        if notes is None:
            print(f"  {f.name}: SKIP/ERR")
        elif notes:
            print(f"  {f.name}: {len(notes)} conflict(s)")
            for n in notes[:3]:
                print(f"    - {n[:100]}")
        else:
            print(f"  {f.name}: clean")
