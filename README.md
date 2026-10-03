# verilog_script

**English** | [简体中文](README.zh-CN.md)

Emacs `verilog-mode` AUTO expansions and the `automatic.vim` command set,
rewritten as a self-contained Vim plugin backed by a small Python package.

No pip dependencies (Python standard library only), no emacs required.
The buffer never needs to be saved first — commands run **asynchronously**
on the live buffer: key pipeline steps log into `:messages` while they run
(so a big SoC top visibly makes progress instead of looking hung), and the
buffer updates in place on completion (undo history, marks and folds
survive). Edits made while a command runs abort that apply, so nothing is
ever clobbered; `VERILOG_TOOLING_QUIET=1` silences the progress logs.

## Requirements

- Vim 8.2+
- **Python ≥ 3.8** in `PATH` (or point `g:verilog_tooling_python` at one)

## Install

### Plugin managers (recommended)

```vim
" vim-plug
Plug 'marshaha/verilog_script'

" Vundle
Plugin 'marshaha/verilog_script'

" packer.nvim
use 'marshaha/verilog_script'
```

### One-click manual install

```bash
git clone https://github.com/marshaha/verilog_script.git
cd verilog_script
./install.sh            # copies plugin/ and python/ into ~/.vim
./install.sh --check    # environment check only
```

## Quick start

Put AUTO markers where you want generated text, then press `<leader>a`
(that is `\a` with the default leader, `-a` with `let mapleader = "-"`)
or run `:AALL`:

```verilog
module top (
    input        clk,
    input  [7:0] din,
    output       done
);
/*autodef*/

sub u_sub (/*autoinst*/);

endmodule
```

becomes:

```verilog
module top (
    input        clk,
    input  [7:0] din,
    output       done
);
/*autodef*/
// Define io wire here
wire                                    clk;
wire         [7:0]                      din;
reg                                     done;   // undriven output -> reg
// Define flip-flop registers here
// ...

sub u_sub (/*autoinst*/
// Outputs
.dout                                   (dout[7:0]              ),
// Inputs
.clk                                    (clk                    ),
.din                                     (din[7:0]               ));
// End of automatics

endmodule
```

(the default connection is the port name itself, range included — no
suffixes are invented)

Re-running is idempotent; `KI`/`KAR`/`KADT` collapse a region back to its
marker.

## Commands

| Command | What it does | Key |
|---|---|---|
| `AALL` | full AUTO set in one pass, emacs `verilog-batch-auto` order (see below) | `<leader>a` |
| `EAI` / `EAP` | verilog-mode AUTOINST / AUTOINSTPARAM | `<leader>eai` `eap` |
| `AIT` | (re)build instance connections from the module definition | `<leader>ait` |
| `AIU` / `AIU1` | minimal-diff instance updates (keep manual connections) | `<leader>aiu` `aiu1` |
| `KI` | collapse an instance back to the `/*autoinst*/` stub | `<leader>d` |
| `AD` / `ADT` | regenerate `/*autodef*/` wire/reg/integer/genvar declarations | `<leader>ad` `adt` |
| `KADT` | collapse the `/*autodef*/` region | |
| `AR` | regenerate `/*autoarg*/` header port lists | `<leader>ar` |
| `KAR` | collapse the `/*autoarg*/` region | |
| `AW` / `AREG` | AUTOWIRE / AUTOREG | `<leader>aw` `arg` |
| `AIO` | AUTOOUTPUT + AUTOINPUT + AUTOINOUT (wrapper port generation) | `<leader>aio` |
| `ASEN` | AUTOSENSE / AS — sensitivity list from signals read in the always block | `<leader>as` |
| `ARST` | AUTORESET — reset assignments for every signal driven in a reset block | `<leader>arst` |
| `AIM` / `AIC` / `AII` | AUTOINOUTMODULE / AUTOINOUTCOMP / AUTOINOUTIN — copy I/O from another module | |
| `AIMP` / `AIP` | AUTOINOUTMODPORT / AUTOINOUTPARAM — I/O from an interface modport / params from a module | |
| `AAMP` | AUTOASSIGNMODPORT — assignments into an interface modport | |
| `AOE` | AUTOOUTPUTEVERY — every signal becomes an output | |
| `ARI` | AUTOREGINPUT — `reg` for undeclared AUTOINST input nets | |
| `AASC` | AUTOASCIIENUM — ASCII decode register for an enum state vector | |
| `ALGC` | AUTOLOGIC — AUTOWIRE with `logic` declarations | |
| `ATIE` | AUTOTIEOFF — tie undriven outputs to deasserted | `<leader>atie` |
| `AUNU` | AUTOUNUSED — comma list of unused inputs/inouts (for `_unused_ok`) | |
| `AUND` | AUTOUNDEF — `` `undef `` every file-local `` `define `` | |
| `AIL` / `AILL` | AUTOINSERTLISP / AUTOINSERTLAST — insert shell-command output (`!cmd`) | |
| `AINJ` | inject AUTO markers into legacy code, then run AALL | `<leader>ainj` |
| `ADIF` | diff current buffer against full AUTO expansion (preview window) | `<leader>adif` |
| `ATLINT` | warn about unused AUTO_TEMPLATE lines (quickfix) | |
| `AF` | format: ports, wire/reg, parameter/localparam, instances | `<leader>af` |
| `AIF` `APF` `ADF` | individual format passes | `<leader>aif` `apf` `adf` |
| `AM` `AME` | instance stub from the word under the cursor | `<leader>am` `ame` |
| `APM` `AFM` | `/*autopara*/` / `/*autofsm*/` expansion | |
| `AH` / `ATpl {file}` | file header / new file from template | |
| `BPN` `BP` `BA` | always-block snippets | `<leader>bpn` `bp` `ba` |

`{count}` before EAI/AIT/AIU/AIU1 selects the Nth `/*autoinst*/` instance
(`:1AIT` → the first one only).

Every command reports what it did, e.g.
`[verilog_tooling] eai: line 515: 1005 -> 937 line(s)` (or `no changes`).

### Performance (large SoC tops)

`AALL` runs the whole pipeline in **one Python process** with the module
table shared across all eight passes; module files are located via cached
directory listings and read on a thread pool (NFS-friendly — no
stat-per-module-dir storm). Measured on a 199-file project: 3.4× faster
than the eight separate commands, byte-identical output.

### `include handling

Every analysis that reads Verilog (parameter/`define collection, module
port/interface parsing, AUTOINST/AUTOINSTPARAM, AUTOWIRE, autodef) sees
through `` `include "x"`` — including the inline ``#(`include "m_params.svh")``
parameter-list form, and constant-expression values (`A+B`, `$clog2(X)`,
`(X==1) ? 1 : $clog2(X)`) fold to integers. Missing files keep the
directive untouched (one warning). The output text always keeps the
original `` `include`` line.  This is deliberately stronger than Emacs:
verilog-mode's `verilog-auto-read-includes` defaults to nil, reads only
`` `define`` macros, and only from line-start directives.

**Performance**: library scans (interface lookup, module resolution,
include expansion) are cached and thread-pooled.  Interface declarations
are raw-regex scanned across `.v` / `.sv` / `.svh` / `.vh` files, so an
interface living in an `.svh` header is found without any include
expansion; ``// verilog-auto-read-includes:t`` at the bottom of the file
additionally follows `` `include`` edges (for oddly-named include files)
via a basename index — cheap either way.  On a real project with 594 `-y`
dirs and 2200+ files, `-a` takes about 4 seconds.

## Command reference

### AALL — everything, in the right order

Runs the emacs `verilog-batch-auto` sequence in a single Python process:

AIL → EAP → EAI → AASC → AIMP → AIM → AIC → AII → AIP → AIO → ATIE →
AUND → AAMP → ALGC → AW → AREG → ARI → AOE → ASEN → ARST → AUNU → AD →
AR → AILL → AF

(AUTOINSERTLISP, AUTOINSTPARAM, AUTOINST, AUTOASCIIENUM, AUTOINOUTMODPORT,
AUTOINOUTMODULE, AUTOINOUTCOMP, AUTOINOUTIN, AUTOINOUTPARAM,
AUTOOUTPUT/AUTOINPUT/AUTOINOUT, AUTOTIEOFF, AUTOUNDEF, AUTOASSIGNMODPORT,
AUTOLOGIC, AUTOWIRE, AUTOREG, AUTOREGINPUT, AUTOOUTPUTEVERY, AUTOSENSE,
AUTORESET, AUTOUNUSED, autodef, AUTOARG, AUTOINSERTLAST, format).
The output is byte-identical to running the commands in that order, but
module files are resolved and read only once. `g:verilog_tooling_eai_flags`
(e.g. `--sort`) is honored. Markers you don't use are no-ops, so AALL is
safe on any buffer.

### EAI — verilog-mode AUTOINST

Expands `/*AUTOINST*/`: discards the previous expansion and connects
every pin of the submodule, grouped into `// Interfaces` / `// Outputs` /
`// Inouts` / `// Inputs` sections in declaration order (`--sort` sorts
within each group). The default connection is the port name with its
range — `.dout (dout[7:0])`. Notes:

- EAI is a **full reset** (emacs semantics): to keep a custom connection,
  put it in an `AUTO_TEMPLATE` — template-driven pins are marked
  `// Templated`. Use AIU/AIU1 instead to preserve hand-written
  connections in place.
- Pins already connected **before** the marker line are kept.
- `/*AUTOINST("regex")*/` keeps only matching pins; a `?!` prefix
  excludes them (case-insensitive; emacs and Python regexp dialects).
- SystemVerilog `.*` instances expand when star expansion is enabled;
  interface ports connect as `.bus (bus.master)`.
- Instances whose module file cannot be found are skipped with a warning
  listing the searched dirs.

### EAP — AUTOINSTPARAM

Fills the `#(...)` parameter list (`/*AUTOINSTPARAM*/`, or new instances
created by AIT/EAI). See the priority rules in
[AUTOINSTPARAM (EAP)](#autoinstparam-eap) below.

### AIT — (re)build an instance

automatic.vim AutoInst: kills the current connections and regenerates
them from the module definition (identity connections, or AUTO_TEMPLATE
where declared). Use it on a stub `fifo u0_fifo (/*autoinst*/);` to
build the full pin list, or to force a clean rebuild. An `--oneline`
comment on the instance line packs everything onto one line.

### AIU / AIU1 — minimal-diff updates

For daily "the submodule changed" work:

- `AIU1` keeps every existing line, appends new ports before `);`
  (marked `// INST_NEW`) and comments out deleted ones
  (marked `// INST_DEL`).
- `AIU` does the same update but rewrites the instance in module port
  order, reusing each surviving connection line verbatim — hand-edited
  `.port (custom_sig)` connections keep their text. An `AUTO_TEMPLATE`
  entry still wins for the ports it declares.

### KI — collapse an instance

Deletes the generated pin list, leaving the `mod inst (/*autoinst*/);`
stub. The next EAI/AIT rebuilds from scratch.

### AD / ADT — declare every undeclared signal

Regenerates the `/*autodef*/` region: io wires, flip-flop and
combinational registers, assign wires, instance-driven wires, and
for-loop variables (`integer`/`genvar`), with widths inferred from
drivers. Undriven outputs become `reg`. See [/*autodef*/
(AD/ADT)](#autodef-adadt) below for the full rules. `KADT` collapses the
region back to the marker.

### AR — header port list

Regenerates `/*autoarg*/` in the module header from the port
declarations: `//Inputs` / `//Outputs` / `//Inouts` sections, names
packed 4 spaces in and wrapped past column 40. Handles header-style
declarations (`input clk,`), several declarations on one line
(`input a, input b`), cross-line declarations, and unpacked dimensions
(`val[3:0]`). A misplaced marker (outside the header) is left untouched.
`KAR` collapses the list back to the marker.

**Inout inference**: a port-list name with only a `wire` declaration or
no direction at all is not a legal Verilog port and would be dropped
(with a per-name warning) — unless it connects to an **inout pin** of an
instantiated module (a pad, e.g. `.GPIO0_A00 (GPIO0_A00)`). Then AR
keeps it in the `//Inouts` section and emits an `inout wire name;` body
declaration so the port is legal — and AUTOWIRE/autodef never declare a
`wire` for it. Other direction-less names are still dropped, each listed
on stderr / in `:messages`.

### AW / AREG — AUTOWIRE / AUTOREG

`/*AUTOWIRE*/` declares wires for nets driven by instance outputs;
`/*AUTOREG*/` declares `reg` for module outputs with no driver. See
[AUTOWIRE / AUTOREG](#autowire--autoreg-awareg) below.

### ASEN / ARST — AUTOSENSE / AUTORESET

`always @(/*AUTOSENSE*/)` (or `/*AS*/`) rewrites the sensitivity list from
the signals the block actually reads — signals assigned inside the block
are excluded, `/*AUTO_CONSTANT(`x) */` excludes `` `define``s, memories get
a `/*memory or*/` note. `/*AUTORESET*/` inside a reset branch emits
`// Beginning of autoreset for uninitialized flops` with
`sig <= width'h0;` for every signal driven elsewhere in the always block
but not manually reset before the marker (`<=` vs `=` follows the block's
style; active-low names per `verilog-active-low-regexp` reset to 1).

### AIM / AIC / AII / AIMP / AIP — copy I/O from elsewhere

- `/*AUTOINOUTMODULE("Mod"[,"re"])*/` (AIM): copy input/output/inout
  declarations from another module — the null-shell workhorse.
- `/*AUTOINOUTCOMP("Mod"[,"re"[,"not-re"]])*/` (AIC): same, complemented
  (inputs become outputs) — for testbenches.
- `/*AUTOINOUTIN("Mod"[,"re"])*/` (AII): same, everything as input — for
  monitors.
- `/*AUTOINOUTMODPORT("If","mp-re"[,"re"[,"prefix"]])*/` (AIMP): copy I/O
  from an interface modport.
- `/*AUTOINOUTPARAM("Mod"[,"re"])*/` (AIP): copy `parameter` declarations
  (value-less, SystemVerilog-2009 style).

Inside a module header they emit Verilog-2001 comma style, otherwise
1995 `;` declarations. `?!` prefix on a regexp excludes matches.

### AAMP — AUTOASSIGNMODPORT

`/*AUTOASSIGNMODPORT("If","mp-re","inst"[,"re"[,"prefix"]])*/` builds
`assign` statements wiring the modport signals to/from the interface
instance — for UVM verification modules.

### AOE / ARI — AUTOOUTPUTEVERY / AUTOREGINPUT

`/*AUTOOUTPUTEVERY[("re")]*/` declares every non-input signal an output
(keeps synthesis from optimizing signals away). `/*AUTOREGINPUT*/`
declares `reg` for undeclared nets feeding AUTOINST input pins
(`// To <inst> of <Mod>.v`), handy for top-level test shells.

### AASC — AUTOASCIIENUM

`/*AUTOASCIIENUM("sig", "ascii_sig"[,"prefix"[,"onehot"]])*/` builds an
ASCII decode register for an enum state vector: parameters tagged
`// auto enum <name>` (or `synopsys enum`) define the states, the signal
tagged `/* auto state_vector <sig> */` selects the vector. Emits
`reg [8*N-1:0] ascii_sig; // Decode of sig` plus the `always @(sig)`
case decoder (`"%Err"` default).

### ALGC — AUTOLOGIC

`/*AUTOLOGIC*/` is AUTOWIRE declaring `logic` instead of `wire`. A file-local
`// verilog-auto-wire-type: "logic"` switches `/*AUTOWIRE*/` the same way.

### ATIE — AUTOTIEOFF

`/*AUTOTIEOFF*/` ties every unterminated module output to deasserted:
`wire [w:0] o = w'h0;` (`~w'h0` for active-low names, `w'sh0` for signed).
Outputs already declared, driven by AUTOINST, or matching
`verilog-auto-tieoff-ignore-regexp` are skipped. The classic stub-module
companion to `AUNU`.

### AUNU — AUTOUNUSED

`/*AUTOUNUSED*/` expands inline to the comma-separated list of unused
input/inout signals — designed for
`wire _unused_ok = &{1'b0, /*AUTOUNUSED*/ 1'b0};` so one pragma silences
all unused warnings. `verilog-auto-unused-ignore-regexp` excludes names.

### AUND — AUTOUNDEF

`/*AUTOUNDEF[("re")]*/` emits `` `undef `` for every `` `define `` seen
since the previous AUTOUNDEF (already-undef'd names are skipped, so
`` `ifdef NEVER `` guards work), sorted, optionally regexp-filtered —
keeps file-local defines out of the global namespace.

### AIL / AILL — AUTOINSERTLISP / AUTOINSERTLAST

`/*AUTOINSERTLISP(!command args)*/` runs the shell command and inserts its
stdout into a `// Beginning of automatic insert lisp` region, before (AIL)
or after (AILL) all other AUTOs. Emacs evaluates elisp here; this port runs
shell instead — the documented difference (elisp `defun`s are not
supported, mirroring the AUTO_TEMPLATE `@"..."` subset rule).

### AINJ — inject AUTOs into legacy code

Inserts `/*AUTOARG*/` into module headers, `/*AS*/` into always blocks
whose hand-written sensitivity list already matches, and `/*AUTOINST*/`
into pin lists (deleting `.x(x)` identity pins), then runs the full AALL
pipeline — the `verilog-inject-auto` workflow for bringing old files
under AUTO control.

### ADIF — diff AUTOs

Expands AUTOs on a copy and shows the unified diff in a preview window
(whitespace-insensitive detection, like `verilog-diff-auto`). Empty output
means the buffer is fully expanded — suitable for a lint/regression check.

### ATLINT — unused AUTO_TEMPLATE lines

Runs the EAI/EAP expansion with hit-tracking and lists template entries
never consumed by any instance in the quickfix window
(`verilog-auto-template-warn-unused`).

### AF family — alignment

Buffer-local formatting (no module files needed):

- `APF` aligns port declarations (`input`/`output`/`inout`),
- `ADF` aligns `wire`/`reg`/`logic` declarations and
  `parameter`/`localparam` (`=` signs aligned),
- `AIF` aligns instance port connections,
- `AF` runs all three.

Idempotent; reports `no changes` when already aligned.

### AM / AME — instance stubs

Turns the word under the cursor into an instance stub on that line:
`fifo` → `fifo u0_fifo (/*autoinst*/);` (AM, automatic.vim style) or
the emacs-flavored stub (AME). The instance index counts the module's
previous instances in the buffer.

### APM / AFM — parameter / FSM skeletons

`/*autopara*/ (A, B=2, C)` expands into aligned `parameter`
declarations. `/*autofsm*/ (IDLE,RUN,DONE) state nstate` expands into
state localparams plus the two-always-block FSM skeleton (state register
+ next-state logic), with the state width derived from the state count.

### AH / ATpl — file header & new file

`AH` prepends the file header comment block (`// +FHDR`).
`ATpl foo.v` creates a new file from the project skeleton — a `_tb`/`tb`
suffix produces a testbench skeleton — and opens it. New empty `.v`/`.sv`
buffers get the skeleton automatically (BufNewFile).

### BPN / BP / BA — always-block snippets

Insert an always skeleton at the cursor: `BPN` —
`always @(posedge clk or negedge rst_n)` with `if (!rst_n)` reset
branch; `BP` — `always @(posedge clk)`; `BA` — combinational
`always @(*)`.

## Default key mappings

The plugin installs these normal-mode leader mappings at `VimEnter` — so a
`mapleader` set anywhere in your vimrc is honored — and only when the lhs
has no mapping yet, so your own mappings always win:

| Key | Command | Key | Command |
|---|---|---|---|
| `<leader>a` | `AALL` | `<leader>af` | `AF` |
| `<leader>ad` | `AD` | `<leader>aif` `adf` `apf` | `AIF` `ADF` `APF` |
| `<leader>adt` | `ADT` | `<leader>am` `ame` | `AM` `AME` |
| `<leader>ait` | `AIT` | `<leader>aw` | `AW` |
| `<leader>aiu` / `aiu1` | `AIU` / `AIU1` | `<leader>arg` | `AREG` |
| `<leader>aio` | `AIO` | | |
| `<leader>ar` | `AR` | `<leader>eai` `eap` | `EAI` `EAP` |
| `<leader>d` | `KI` | `<leader>bpn` `bp` `ba` | `BPN` `BP` `BA` |

Disable the whole set with:

```vim
let g:verilog_tooling_no_mappings = 1
```

## Insert abbreviations (optional, off by default)

`plugin/verilog_abbrev.vim` ports automatic.vim's global insert
abbreviations, scoped to `*.v`/`*.vh`/`*.sv`/`*.svh` buffers only:

| Type | Expands to |
|---|---|
| `<=` | `<= #\`RD` |
| `beg` | `begin` |
| `begni` (typo) | `begin` |

**Disabled by default** — enable in your vimrc:

```vim
let g:verilog_abbrev_enable = 1
```

## How module files are found

EAI/AIT/AW need the submodule's source file to read its ports. Search order:

1. `verilog-library-directories` / `verilog-library-flags` in the buffer's
   Local Variables block (relative paths resolve against the **real file's
   directory**, `~` and a leading `$VAR` expand)
2. `-f file.vc` library files listed there (recursively)
3. the directory of the file being edited, and modules defined in the same
   buffer
4. `g:verilog_tooling_libdirs` / CLI `-y` dirs

A typical Local Variables block at the bottom of a file:

```verilog
// Local Variables:
// verilog-library-directories:("../pmu" "../por_rst" "../../common/clk/rtl/com")
// verilog-library-flags:("-f ../mm_file_dir.vc")
// verilog-auto-inst-param-value:t
// End:
```

And the `.vc` file it points at (one entry per line; `#`/`//` comments ok):

```
# mm_file_dir.vc — module search list
-y  $PROJ_DIR/design/rtl/axi_1_to_n
-y  $PROJ_DIR/design/rtl/mm_cmd_dma
-y  $PROJ_DIR/design/rtl
+incdir+$PROJ_DIR/design/include
-v  $PROJ_DIR/design/rtl/legacy/pll_wrap.v
+libext+.v+.sv
```

Supported vc entries: `-y dir`, `+incdir+dir`, `-I dir`, `-v file`,
`+libext+.v+.sv…`. If a module cannot be found, its instance is skipped
with a `[verilog_tooling]` warning listing the searched dirs.

## AUTO_TEMPLATE (EAI/EAP)

Templates customize connections per instance. They live in a comment above
the instance (or in the file named by `verilog-inst-file:`):

```verilog
/* mm_cdma_parse AUTO_TEMPLATE (
    .CDMA_REQ_NUM        (CDMA_INTERNAL_NUM),
    .cmd_apb_fifo_rdata  (cmd_apb_fifo_rdata_r[]),
    .cdma_\(ar\|aw\)id_s (cdma_\1id_internal[1:0]),
    .int_req             (int_req_@),
) */
mm_cdma_parse u_parse (/*autoinst*/);
```

- `@` in a **connection** expands to the instance number (the first digit
  group of the instance name; override what `@` matches with
  `AUTO_TEMPLATE "my_re_\([0-9]+\)"`)
- `@` in a **port pattern** matches a digit group: `.port_@ (sig_@)` covers
  `port_0 … port_3`
- `[]` expands to the connected port's declared packed range
  (`rdata[7:0]`; removed for scalar ports)
- `\(...\)` groups in a regexp port pattern back-substitute as `\1`, `\2`…
- **both Emacs and Python regexp dialects work — auto-detected** (here and
  in the `AUTO_TEMPLATE "regexp"`, `/*AUTOINST("regex")*/` filter and
  `verilog-typedef-regexp`): Emacs `\(...\)`/`\|` and Python `(...)`/`|`
  can even mix; Python-style `\g<1>` backrefs work alongside `\1`
- `@"expr"` evaluates an expression — Python (`@"'pre_%d' % @"`), or
  parenthesised elisp (`@"(downcase vl-name)"`; supported forms:
  `substring`, `downcase`, `concat`, `if`, `equal`, arithmetic, `let`,
  `setq`; `vl-name`, `vl-cell-name`, `vl-width`, `vl-dir` are bound)
- `/*AUTO_LISP(expr)*/` before the marker evaluates Python bindings that
  `@"..."` expressions can reference

Without a template entry, a port connects to a same-named net (range
included); if the net is not declared yet, AW/AD declare it for you.
SystemVerilog `interface` ports (with modports) are supported:
`cpu_bus.master bus` connects as `.bus (bus.master)`.
`.*` instances expand as AUTOINST when star expansion is enabled.

## AUTOINSTPARAM (EAP)

`/*AUTOINSTPARAM*/` (and the `#(...)` of new instances from AIT/EAI) gets
parameter connections in this priority:

1. an `AUTO_TEMPLATE` entry for the parameter
2. a same-named parameter/localparam of the parent module (passed through
   symbolically — values are **not** replaced by numbers, so instantiating
   modules further up still works)
3. the submodule's default value, when it is visible in the parent
4. otherwise the parameter is omitted (the submodule default applies)

With `verilog-auto-inst-param-value:t` in Local Variables (or CLI
`--param-value`), the instance's `#(...)` overrides are additionally
substituted into port widths and template connections (constant
expressions and `$clog2` are folded).

## /*autodef*/ (AD/ADT)

Regenerates every undeclared signal into fixed sections, inferring widths
from drivers (literals, part-selects, comparisons, signal links, submodule
port widths, `+:`/`-:` indexed part-selects). Widths stay symbolic when
they reference parameters/`` `define``s.

```
// Define io wire here            ports without explicit wire/reg
// Define flip-flop registers here    LHS of <= in clocked always
// Define combination registers here   LHS of = in other always
// Define wires here                  LHS of assign
// Define inst wires here             nets driven by instance outputs
// Define integer here                for-loop variables
// Unresolved define signals here     could not infer (as // comments)
```

Type rules:

- `always` block LHS → `reg`; `assign` / instance-driven → `wire`
- an **undriven `output` → `reg`** (you will drive it from an always block
  next; matches AREG). Inputs/inouts and driven outputs stay `wire`
- **for-loop variables are declared automatically**: `genvar` in
  `generate` loops, `integer` in procedural loops (always/function/task),
  including two-level nested loops; loop-indexed LHS like `mem[i][j]` or
  `fifo[i*W +: W]` contribute unpacked dimensions and grown widths
- hand-written declarations are kept (a provably stale numeric width is
  grown in place); append `//DT` ("don't touch") to exempt one
- SystemVerilog `logic` is understood everywhere

## AUTOWIRE / AUTOREG (AW/AREG)

`/*AUTOWIRE*/` declares wires for nets driven by `/*autoinst*/` instance
outputs (with a `// From u_x of mod.v` comment). `/*AUTOREG*/` declares
`reg` for module outputs that have no driver (assign/always/instance).
Both skip anything already declared and resolve widths symbolically.

## AUTOINPUT / AUTOOUTPUT / AUTOINOUT (AIO)

The wrapper-module trio from verilog-mode (`:AIO` runs AUTOOUTPUT then
AUTOINPUT then AUTOINOUT, and `AALL` includes all three):

- `/*AUTOINPUT*/` declares an `input` port for every net feeding an
  instance input that is not declared or driven inside the module
  (`// To u_x of mod.v` comment);
- `/*AUTOOUTPUT*/` declares an `output` port for every net driven by an
  instance output that is not already a port and does not feed another
  instance (those stay internal — AUTOWIRE territory);
- `/*AUTOINOUT*/` declares an `inout` port for every net on an instance
  inout that is not already a port (`// To/From u_x of mod.v` comment).
  Without the marker, inout-connected nets stay internal wires — AUTOWIRE
  declares them with a `// To/From` comment.

Placed inside the module header parens they expand Verilog-2001 style
(comma-separated, with verilog-mode's open/close comma repair); in the
body, Verilog-1995 style (`;`). Each marker accepts a regexp filter —
`/*AUTOINPUT("^i_")*/`, prefix `?!` to invert — and
`verilog-auto-input-ignore-regexp` / `verilog-auto-output-ignore-regexp`
file-local variables are honored. Widths come from the submodule port with
the instance's `#(...)` parameter overrides substituted (verilog-mode
itself only gets widths right for AUTOINST-rewritten connections; a marker
immediately after the header `(` expands normally where emacs errors out).
An undeclared assign-driven net IS made an input (verilog-mode parity —
promote it to a port and drop the assign yourself); concat/expression
connections are skipped unless `verilog-auto-ignore-concat` is nil.

File-local variables (in the `// Local Variables:` section) tuning AIO,
AUTOWIRE and friends:

- `// verilog-auto-ignore-concat: t` — **our default** (emacs defaults to
  nil): pin connections in `{...}` or `(...)` are ignored, which is
  exactly the "wrap it in {} to exempt it" workflow. Set it to `nil` to
  extract the identifiers instead (nested concats, unary operators and
  casts are stripped; an element keeps its own `[msb:lsb]` width);
- `// verilog-auto-wire-comment: nil` — suppress the `// To`/`// From`
  comments on generated declarations.

AIO and `/*autoarg*/`: with `/*autoarg*/` in the header, put the
AUTOINPUT/AUTOOUTPUT markers in the **body** — autoarg then packs the
generated port names into the header (the canonical verilog-mode layout).
When the AIO markers are in the header themselves, autoarg leaves that
header alone (a name list would duplicate the full declarations).

## Layout

```
plugin/verilog_tooling.vim   Vim front-end (auto-loaded)
plugin/automatic.vim         header/waveform snippets (BPN/BP/BA, AddClk/AddSig/AddBus)
plugin/verilog_abbrev.vim    opt-in buffer-local iabbrevs for .v/.vh/.sv/.svh (off by default)
python/verilog_tooling/      the Python package (stdlib only)
doc/verilog_tooling.txt      vim help
install.sh                   one-click installer / env checker
```

Optional settings:

```vim
let g:verilog_tooling_python = '/usr/bin/python3'   " interpreter override
let g:verilog_tooling_libdirs = ['/path/to/rtl']    " extra -y search dirs
let g:verilog_tooling_eai_flags = ['--sort']        " extra EAI flags
let g:verilog_tooling_no_mappings = 1               " no default mappings
let g:verilog_abbrev_enable = 1                     " opt-in insert abbrevs (<=>`<= #`RD`, beg, begni)
let g:verilog_tooling_interfaces = ['axera_apb_interface']  " user-known SV interface types
let g:verilog_tooling_quiet = 1                     " no pipeline progress logs
```

Progress logs (aall step timings in `:messages`) are **on by default**;
three ways to turn them off, pick whichever fits:

```vim
" 1. vimrc — the vim-native switch
let g:verilog_tooling_quiet = 1

" 2. vimrc or live in vim — the raw environment variable (children inherit it)
let $VERILOG_TOOLING_QUIET = 1
```

```bash
# 3. shell — for terminal/batch runs (make permanent in ~/.bashrc or ~/.zshrc)
export VERILOG_TOOLING_QUIET=1
# or per command:
VERILOG_TOOLING_QUIET=1 python3 -m verilog_tooling.inst aall -i top.v -o out.v -y .
```

With logs quieted, only the plugin's own `running ...` / completion summary
messages remain.

`g:verilog_tooling_interfaces` lists SystemVerilog interface type names the
library scan cannot find (the file is not under any `-y` dir). Ports like
`axera_apb_interface.master apb` then parse as interface ports for
EAI/AIT/AIU — EAI puts them in the `// Interfaces` section with a
`name.modport` connection — and are never mistaken for wires by
AW/AREG/AD/AIO.
