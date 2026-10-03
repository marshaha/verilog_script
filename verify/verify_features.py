#!/usr/bin/env python3
"""Feature-isolated verification: run each new AUTO command standalone and
compare ONLY its generated region against the emacs golden's region.
This excludes the pre-existing AR/AF formatter deviations."""
import subprocess, sys, re, tempfile, os
from pathlib import Path

WS = Path(__file__).parent.parent
TESTS = Path("/tmp/vcmp/verilog-mode/tests")
GOLDEN = WS / "verify" / "golden"

# (test_file, module, command_args, region_start_re, region_end_re)
CASES = [
    # AUTOSENSE
    ("autosense.v", "sense", ["asense"], r"always @\(.*AUTOSENSE", r"\) begin"),
    # AUTORESET
    ("autoreset_reed.v", "sense", ["areset"], r"AUTORESET", r"End of automatics"),
    # AUTOINOUTMODULE
    ("autoinoutmodule.v", "xfer", ["ainoutmodule"], r"AUTOINOUTMODULE", r"End of automatics"),
    # AUTOINOUTCOMP
    ("autoinoutcomp.v", "xfer", ["ainoutcomp"], r"AUTOINOUTCOMP", r"End of automatics"),
    # AUTOINOUTIN
    ("autoinoutin.v", "xfer", ["ainoutin"], r"AUTOINOUTIN", r"End of automatics"),
    # AUTOINOUTPARAM
    ("autoinoutparam.v", "xfer", ["ainoutparam"], r"AUTOINOUTPARAM", r"End of automatics"),
    # AUTOASCIIENUM
    ("autoasciienum_ex.v", "misc", ["aascii"], r"AUTOASCIIENUM", r"End of automatics"),
    # AUTOLOGIC
    ("autologic.sv", "misc", ["alogic"], r"AUTOLOGIC", r"End of automatics"),
    # AUTOTIEOFF
    ("autotieoff_signed.v", "misc", ["atieoff"], r"AUTOTIEOFF", r"End of automatics"),
    # AUTOUNUSED
    ("autounused.v", "misc", ["aunused"], r"AUTOUNUSED", r"1'b0};"),
    # AUTOUNDEF
    ("autoundef.v", "misc", ["aundef"], r"AUTOUNDEF", r"End of automatics"),
    # AUTOOUTPUTEVERY
    ("autooutputevery_example.v", "xfer", ["aoutputevery"], r"AUTOOUTPUTEVERY", r"End of automatics"),
    # AUTOREGINPUT
    ("autoreginput.v", "xfer", ["areginput"], r"AUTOREGINPUT", r"End of automatics"),
]

def extract_region(lines, start_re, end_re):
    """Extract lines from start_re match through end_re match."""
    out, in_region = [], False
    for l in lines:
        if not in_region and re.search(start_re, l):
            in_region = True
        if in_region:
            out.append(l.rstrip())
            if re.search(end_re, l):
                break
    return out

def run_case(test_file, module, cmd_args):
    src = TESTS / test_file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.v', delete=False) as f:
        f.write(src.read_text())
        tmp = f.name
    try:
        # run from a temp copy; libdir = tests dir for module resolution
        cmd = [sys.executable, "-m", f"verilog_tooling.{module}"] + cmd_args + [
            "-i", tmp, "-o", tmp + ".out", "-y", str(TESTS)]
        r = subprocess.run(cmd, cwd=WS, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(WS / "python")},
                          timeout=60)
        if r.returncode != 0:
            return None, f"exit {r.returncode}: {r.stderr[:200]}"
        return Path(tmp + ".out").read_text().splitlines(), None
    except Exception as e:
        return None, str(e)
    finally:
        os.unlink(tmp)
        if os.path.exists(tmp + ".out"):
            os.unlink(tmp + ".out")

def main():
    passed, failed = [], []
    for test_file, module, cmd_args, start_re, end_re in CASES:
        out_lines, err = run_case(test_file, module, cmd_args)
        if out_lines is None:
            failed.append((test_file, f"RUN FAIL: {err}"))
            continue
        golden_lines = (GOLDEN / test_file).read_text().splitlines()
        got = extract_region(out_lines, start_re, end_re)
        want = extract_region(golden_lines, start_re, end_re)
        if got == want:
            passed.append(test_file)
        else:
            # show first diff
            diff = []
            for i, (g, w) in enumerate(zip(got, want)):
                if g != w:
                    diff.append(f"  line {i}: got {g!r} want {w!r}")
                    if len(diff) >= 3:
                        break
            if len(got) != len(want):
                diff.append(f"  length: got {len(got)} want {len(want)}")
            failed.append((test_file, "\n".join(diff) if diff else "empty region"))
    print(f"PASSED {len(passed)}/{len(CASES)}")
    for f in passed:
        print(f"  [OK] {f}")
    for f, why in failed:
        print(f"  [FAIL] {f}\n{why}")

if __name__ == "__main__":
    main()
