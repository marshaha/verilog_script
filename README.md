# verilog_script

**English** | [简体中文](README.zh-CN.md)

Emacs `verilog-mode` AUTO expansions and the `automatic.vim` command set,
rewritten as a self-contained Vim plugin backed by a small Python package.

No pip dependencies (Python standard library only), no emacs required.
Commands run **asynchronously** on the live buffer (no save needed);
progress logs go to `:messages`, and edits made while a command runs
abort its apply, so nothing is ever clobbered. `VERILOG_TOOLING_QUIET=1`
silences the progress logs.

## Requirements

- Vim 8.2+
- **Python ≥ 3.7** in `PATH` (or point `g:verilog_tooling_python` at one)

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

## Configuration

Optional Vim settings — set only what you need:

```vim
let g:verilog_tooling_python = '/usr/bin/python3'   " interpreter override
let g:verilog_tooling_libdirs = ['/path/to/rtl']    " extra -y search dirs
let g:verilog_tooling_eai_flags = ['--sort']        " extra EAI flags
let g:verilog_tooling_no_mappings = 1               " no default mappings
let g:verilog_abbrev_enable = 1                     " opt-in insert abbrevs
let g:verilog_tooling_interfaces = ['axera_apb_interface', 'axera_axi_interface']  " user-known SV interface types
let g:verilog_tooling_quiet = 1                     " no pipeline progress logs
```

`g:verilog_tooling_interfaces` lists SystemVerilog interface type names
the library scan cannot find (the file is not under any `-y` dir). Ports
like `axera_apb_interface.master apb` then parse as interface ports for
EAI/AIT/AIU — EAI puts them in the `// Interfaces` section with a
`name.modport` connection — and are never mistaken for wires by AW/AD.

Progress logs are on by default; silence them with
`let g:verilog_tooling_quiet = 1` in vimrc, `let $VERILOG_TOOLING_QUIET = 1`,
or `VERILOG_TOOLING_QUIET=1` in the shell. With logs quieted, only the
plugin's own `running ...` / completion summary messages remain.

### The leader key

All default mappings hang off Vim's `mapleader`. With the default
leader (`\`), AALL is `\a`, AD is `\ad`, and so on. Set the leader in
your `.vimrc` to whatever you like:

```vim
let mapleader = "-"     " AALL becomes -a, AD becomes -ad, …
```

The plugin installs its mappings at `VimEnter`, so a `mapleader` set
anywhere in your vimrc is honored; a mapping you define yourself always
wins, and `let g:verilog_tooling_no_mappings = 1` disables the whole
default set.

### File-local settings that differ from Emacs

Per-file settings (the `verilog-auto-*` / `verilog-library-*` variables)
live in the buffer itself. Most behave exactly as in verilog-mode — same
names, same defaults; the full table is
[Key concepts §3](#3-file-local-variables--verilog-xxxx-value). These
five do not:

| Variable | Emacs | Here |
|---|---|---|
| `verilog-auto-ignore-concat` | default `nil` | **default on** — `{...}`/`(...)` connections stay off your ports (see [§1](#1-the--exemption--keep-a-signal-off-your-ports-verilog-auto-ignore-concat)) |
| `verilog-auto-read-includes` | default `nil` | **default on** — analyses read through `` `include``s |
| `verilog-autodef-user-boundary` | — (no such variable) | **on** — hand-written declarations are user-owned; conflicts get `//AD_CONFLICT:` (see [§2](#2-dt--never-touch-my-hand-written-declaration)) |
| `verilog-auto-inst-templated-comment` | — (tag always written) | **on**; set `nil` to drop the `// Templated` tag |
| `verilog-auto-python-file` | — (no AUTO_PYTHON) | names a `.py` file of helpers for template `@"..."` expressions — usage: [AUTO_PYTHON and AUTO_LISP](#auto_python-and-auto_lisp) |

## Key concepts

Three conventions drive almost everything the tool generates. Read this
section before the command reference.

### 1. The `{}` exemption — keep a signal off your ports (`verilog-auto-ignore-concat`)

AUTOINPUT / AUTOOUTPUT / AUTOINOUT promote every instance pin connection to
a module port. Wrap a connection in `{...}` (or `(...)`) to say *this signal
is hand-managed — never put it on my port list*:

```verilog
sub u_sub (
    .dqs ({user_dqs}),   // stays a local signal — no port is created
    .dq  (user_dq),      // becomes a module input/output port
    /*autoinst*/);
```

This is the standard way to keep a manually wired connection — for example
one written **before the `/*autoinst*/` marker** — off the generated ports.
Details:

- the exemption is *candidacy-only*: AUTOWIRE still sees the signal as
  connected internally (it never invents a duplicate wire for it), so you
  declare it yourself — which is the point;
- ours defaults **`verilog-auto-ignore-concat` on** (emacs verilog-mode
  defaults to nil — we deliberately chose the friendlier setting);
- to exempt by name pattern instead, use `verilog-auto-input-ignore-regexp`
  / `verilog-auto-output-ignore-regexp` / `verilog-auto-inout-ignore-regexp`.

### 2. `//DT` — never touch my hand-written declaration

A trailing `//DT` comment marks a hand-written line as user-owned. It is
honored in two places:

**AD / ADT** (declaration regeneration) — a hand-written `wire`/`reg`
outside the `/*autodef*/` region is normally *refreshed* when the inferred
width is provably stale ("width growth"); with `//DT` it is left alone
completely — never rewritten, never flagged:

```verilog
wire [7:0] old_bus; //DT     <- never rewritten, never flagged
```

**AR** (header port list regeneration) — a port written in the module
header with `//DT` survives the `/*autoarg*/` rebuild: the regenerated
list always keeps the name, even when it would otherwise be dropped:

```verilog
module top (
    input  [7:0] user_port, //DT   <- always kept in the /*autoarg*/ list
    /*autoarg*/);
```

Notes:

- module **port declarations themselves** (`input`/`output`/`inout` lines)
  are never rewritten by AD, so `//DT` is not needed there; it guards the
  companion hand-written `wire`/`reg` redeclaration of the port;
- without `//DT` (AD scope): a provably stale width is grown in place; a
  *conflicting* one (not provably stale, e.g. a different symbolic width)
  is kept but annotated `//AD_CONFLICT:` so you can reconcile by hand;
- `verilog-autodef-user-boundary: nil` disables the AD-scope mechanism: AD
  then rewrites hand-written declarations freely.

### 3. File-local variables — `// verilog-xxxx: value`

Settings are read from comments **inside the buffer itself**: either inline
(`// verilog-auto-inst-param-value:t` anywhere in the file) or in the
classic `// Local Variables:` block at the end. Same format as emacs
verilog-mode, so files round-trip between the two tools.

Supported variables (all optional; bool defaults in **bold**):

| Variable | Values (default) | What it controls |
|---|---|---|
| `verilog-auto-ignore-concat` | bool (**on**) | the `{}`/`()` exemption above — skip those connections for AUTOINPUT/OUTPUT/INOUT candidacy |
| `verilog-auto-input-ignore-regexp` / `verilog-auto-output-ignore-regexp` / `verilog-auto-inout-ignore-regexp` | elisp/Python regexp | name patterns excluded from the generated ports |
| `verilog-auto-wire-comment` | bool (**on**) | `nil` suppresses the `// To u_…` / `// From u_…` comments on generated declarations |
| `verilog-auto-wire-type` | string | net type AW/AREG declare (e.g. `logic`) |
| `verilog-auto-declare-nettype` | string | net type AUTOINPUT/AUTOOUTPUT ports declare (e.g. `logic`); also lets io declarations with no data type take `<direction> <nettype>` (for `` `default_nettype none``) |
| `verilog-autodef-user-boundary` | bool (**on**) | hand-written declarations are user-owned: conflicts get `//AD_CONFLICT:` instead of a rewrite |
| `verilog-auto-inst-param-value` | bool (off) | substitute `#(...)` parameter values into generated port ranges (`[AW-1:0]` → `[7:0]`); without it parameters stay symbolic |
| `verilog-auto-inst-vector` | bool (**on**) | bus subscripts on default AUTOINST connections; `nil` drops the subscript when the parent declares the net with a matching width, `unsigned` subscripts only unsigned ports |
| `verilog-auto-inst-sort` | bool (off) | sort pins within each direction group |
| `verilog-auto-inst-dot-name` | bool (off) | emit SystemVerilog `.name` shorthand connections |
| `verilog-auto-inst-templated-comment` | bool (**on**) | `nil` drops the `// Templated` tag on template-driven pins |
| `verilog-auto-inst-template-required` | bool (off) | non-nil omits ports that no AUTO_TEMPLATE entry matches |
| `verilog-auto-simplify-expressions` | bool (**on**) | `nil` keeps width expressions verbatim instead of folding constants |
| `verilog-auto-arg-sort` | bool (off) | sorts AUTOARG port names instead of declaration order |
| `verilog-auto-arg-format` | `packed` | `single` puts one AUTOARG port per line |
| `verilog-library-directories` | `("-y" "dir" …)` | module search path (like `-y` CLI flags) |
| `verilog-library-files` | `("file" …)` | extra module library files |
| `verilog-library-flags` | `("-f" "../filelist.vc")` | filelist(s) whose dirs join the search path |
| `verilog-auto-read-includes` | bool (**on**) | also parse `` `include`` files for parameters/`` `define``s/interfaces |
| `verilog-typedef-regexp` | regexp | names matching it are TYPES, never nets (skipped by AW/AD declarations) |
| `verilog-align-typedef-words` | words | extra type words ADF aligns like `reg`/`wire` |
| `verilog-auto-python-file` | filename | Python module for `/*AUTO_PYTHON*/` template expressions |
| `verilog-active-low-regexp` | regexp | active-low reset names (AUTOSENSE/AUTORESET) |
| `verilog-assignment-delay` | string | delay written into AUTORESET assignments |
| `verilog-auto-reset-widths` | see AUTORESET | width policy for AUTORESET assignments |
| `verilog-auto-reset-blocking-in-non` | bool | blocking assignments in the non-reset branch |
| `verilog-auto-sense-defines-constant` | bool | treat `` `define``s as constants in AUTOSENSE |
| `verilog-auto-sense-include-inputs` | bool | include input-only signals in AUTOSENSE |
| `verilog-auto-tieoff-declaration` | string | declaration style for AUTOTIEOFF wires |
| `verilog-auto-tieoff-ignore-regexp` | regexp | signals AUTOTIEOFF leaves alone |
| `verilog-auto-unused-ignore-regexp` | regexp | signals AUTOUNUSED leaves alone |

Boolean values follow the emacs convention: `:nil` turns a variable off,
any other value (`:t`, `:T`, ...) turns it on; matching is
case-insensitive.

## Quick start

Put AUTO markers where you want generated text, then press `<leader>a`
(the leader key is explained in [Configuration](#configuration))
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
| `EA` / `ED` | **emacs backend**: run the original emacs `verilog-batch-auto` / `verilog-batch-delete-auto` over the buffer, then post-format with `AF` (replaces the old emacsauto.vim `FixInstFormat`/`AutoInstFormat` step) | `<leader>o` |
| `AIT` | deprecated: delegates to `EAI` (verilog-mode AUTOINST expansion; warns on stderr) | `<leader>ait` |
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
| `AINJ` | inject AUTO markers into legacy code, then run AALL (the `verilog-inject-auto` workflow) | `<leader>ainj` |
| `ADIF` | diff current buffer against full AUTO expansion (preview window; like `verilog-diff-auto`) | `<leader>adif` |
| `ATLINT` | warn about unused AUTO_TEMPLATE lines (quickfix; `verilog-auto-template-warn-unused`) | |
| `AF` | format: ports, wire/reg, parameter/localparam, instances | `<leader>af` |
| `AIF` `APF` `ADF` | individual format passes | `<leader>aif` `apf` `adf` |
| `AM` `AME` | instance stub from the word under the cursor | `<leader>am` `ame` |
| `APM` `AFM` | `/*autopara*/` / `/*autofsm*/` expansion | |
| `AH` / `ATpl {file}` | file header / new file from template | |
| `BPN` `BP` `BA` | always-block snippets | `<leader>bpn` `bp` `ba` |

`{count}` before EAI/AIT/AIU/AIU1 selects the Nth `/*autoinst*/` instance
(`:1AIT` → the first one only). With no count, AIT/AIU/AIU1 process
the instance under the cursor — the front-end passes `--line` with
the cursor line — while EAI/EAP/KI process every instance. From the
command line, `--which N` (0-based marker index) or `--line N`
(1-based editor line, AIT/AIU/AIU1 only) selects a single instance.

Every command reports what it did, e.g.
`[verilog_tooling] eai: line 515: 1005 -> 937 line(s)` (or `no changes`).

### Performance & `include` handling

`AALL` runs the whole pipeline in **one Python process** with the module
table shared across all passes; module files are located via cached
directory listings and read on a thread pool — on a real project with
594 `-y` dirs and 2200+ files, `-a` takes about 4 seconds.

Every analysis also reads through `` `include "x"`` (parameters,
`` `define``s, interfaces — including the inline ``#(`include "…")``
parameter-list form), and constant expressions (`A+B`, `$clog2(X)`,
`(X==1) ? 1 : $clog2(X)`) fold to integers. Missing files keep the
directive untouched (one warning); the output text always keeps the
original `` `include`` line. This is deliberately stronger than emacs:
verilog-mode reads includes only when asked, and only `` `define``s.
Opt out with `// verilog-auto-read-includes:nil`.

## Featured commands

The six commands below are the daily drivers and are documented in
full. Every other command in the table above is a one-line entry by
design — the marker semantics follow verilog-mode / automatic.vim.

### AALL — everything, in the right order

`<leader>a` (or the CLI's `-a` pipeline) runs the whole AUTO set in the
emacs `verilog-batch-auto` order, in one Python process:

AIL → EAP → EAI → AASC → AIMP → AIM → AIC → AII → AIP → AIO → ATIE →
AUND → AAMP → ALGC → AW → AREG → ARI → AOE → ASEN → ARST → AUNU → AD →
AR → AILL → AF

That is all most files need: put the markers in, press `\a`, done.
Markers you don't use are no-ops, and the output is byte-identical to
running the commands in that order (module files are resolved and read
only once, which is where the speed comes from).

### EA / ED — the emacs backend

Runs the **original** emacs `verilog-batch-auto` (EA) /
`verilog-batch-delete-auto` (ED) over the buffer, then post-formats
with `AF`. This is the drop-in replacement for the old emacsauto.vim
workflow (`FixInstFormat`/`AutoInstFormat`): same results as running
emacs yourself, without leaving Vim. Use it when you want verilog-mode
itself as the engine; use AALL when you want this tool's engine
(async, no emacs process, the `{}` exemption and `//DT` honored).

### AIU / AIU1 — minimal-diff instance updates

For daily "the submodule changed" work:

- `AIU1` keeps every existing line, appends new ports before `);`
  (marked `// INST_NEW`) and comments out deleted ones
  (marked `// INST_DEL`).
- `AIU` does the same update but rewrites the instance in module port
  order, reusing each surviving connection line verbatim — hand-edited
  `.port (custom_sig)` connections keep their text. An `AUTO_TEMPLATE`
  entry still wins for the ports it declares.

Example. The submodule changed: `done` was deleted, `dout`/`cnt` are
new. Starting from:

```verilog
module top;
    sub u_sub (/*autoinst*/
        .clk (rx_clk),
        .din (din),
        .done(done)
    );
endmodule
```

`AIU1` (keep every line in place; `AIU` gives the same update rewritten
in module port order):

```verilog
module top;
    sub u_sub (/*autoinst*/
        .clk (rx_clk),
        .din (din),
//        .done(done) // INST_DEL: port done have deleted 2026-10-08 18:49
    .dout                       (dout[7:0]                                  ), // output // INST_NEW 2026-10-08 18:49
    .cnt                        (cnt[3:0]                                   )  // output // INST_NEW 2026-10-08 18:49
    );
endmodule
```

The hand-written `.clk (rx_clk)` connection survives both; the
timestamp is the run time (`--date` overrides it on the command line).

### AD / ADT — autodef: declare every undeclared signal

`/*autodef*/` is how you stop declaring signals by hand. Put the marker
in the module body; AD regenerates the region into fixed sections,
inferring widths from drivers (literals, part-selects, comparisons,
signal links, submodule port widths, `+:`/`-:` indexed part-selects).
Widths stay symbolic when they reference parameters/`` `define``s.
`KADT` collapses the region back to the marker.

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
  grown in place); append `//DT` ("don't touch") to exempt one — see
  [`//DT`](#2-dt--never-touch-my-hand-written-declaration)
- SystemVerilog `logic` is understood everywhere

```verilog
module top (
    input  wire       clk,
    input  wire [7:0] din,
    output wire [7:0] dout
);
    /*autodef*/
    always @(posedge clk) begin
        q <= din;
    end
    assign dout = q;
endmodule
```

becomes (the undeclared `q` lands in the flip-flop section; the empty
sections stay as comment headers):

```verilog
    /*autodef*/
// Define io wire here
// Define flip-flop registers here
reg          [7:0]                      q;
// Define combination registers here
// Define wires here
// Define inst wires here
// Define integer here
// Unresolved define signals here
// End of automatic define
```

#### User declarations outside the region (`verilog-autodef-user-boundary`)

Declarations outside the `/*autodef*/` region are user-owned. When the
inferred width/dimensions **conflict** with such a declaration, the
default (file-local `// verilog-autodef-user-boundary:t`) keeps the line
and appends a trailing note naming the inferred shape:

```verilog
wire [35:0] ram_rdata;  // my decl  //AD_CONFLICT: infer wire[1:0][35:0]
```

The note is idempotent (re-runs do not duplicate it), disappears once the
declaration is fixed by hand, and its text follows the current inference.
A declaration carrying `//DT` is fully exempt (never rewritten, never
annotated), and nothing is annotated when no width/dimension could be
inferred. With `// verilog-autodef-user-boundary:nil` the old behaviour
returns: a provably wrong declaration is corrected in place (missing or
wrong packed dimensions, provably stale widths).

### AR — header port list

Regenerates `/*autoarg*/` in the module header from the port
declarations: `//Inputs` / `//Outputs` / `//Inouts` sections, names
packed 4 spaces in and wrapped past column 40. Handles header-style
declarations (`input clk,`), several declarations on one line
(`input a, input b`), cross-line declarations, and unpacked dimensions
(`val[3:0]`). A misplaced marker (outside the header) is left untouched.
`KAR` collapses the list back to the marker. Section order is Outputs,
Inouts, Inputs — the emacs order.

```verilog
module top (/*AUTOARG*/);
    input  wire       clk;
    input  wire [7:0] din;
    output wire [7:0] dout;
endmodule
```

becomes:

```verilog
module top (/*AUTOARG*/
    //Outputs
    dout,

    //Inputs
    clk, din
);
    input  wire       clk;
    input  wire [7:0] din;
    output wire [7:0] dout;
endmodule
```

A hand-written header port tagged `//DT` always survives the rebuild —
see [`//DT`](#2-dt--never-touch-my-hand-written-declaration).

**Inout inference**: a port-list name with only a `wire` declaration or
no direction at all is not a legal Verilog port and would be dropped
(with a per-name warning) — unless it connects to an **inout pin** of an
instantiated module (a pad, e.g. `.GPIO0_A00 (GPIO0_A00)`). Then AR
keeps it in the `//Inouts` section and emits an `inout wire name;` body
declaration so the port is legal — and AUTOWIRE/autodef never declare a
`wire` for it. Other direction-less names are still dropped, each listed
on stderr / in `:messages`.

### AF family — alignment

Buffer-local formatting (no module files needed):

- `APF` aligns port declarations (`input`/`output`/`inout`),
- `ADF` aligns `wire`/`reg`/`logic` declarations and
  `parameter`/`localparam` (`=` signs aligned),
- `AIF` aligns instance port connections,
- `AF` runs all three.

Idempotent; reports `no changes` when already aligned. Hand-written
`#(...)` parameter overrides in an instance header are aligned exactly
like pin connections — only the `)) inst (` closer line is never folded
into the last override.

## Formats & module lookup

Our own formats and conventions: how to write the library dirs, the
templates, and the Python helper files.

### How module files are found — writing the dirs

EAI/AIT/AW need the submodule's source file to read its ports. Search order:

1. `verilog-library-directories` / `verilog-library-flags` in the buffer's
   Local Variables block (relative paths resolve against the **real file's
   directory**, `~` and a leading `$VAR` expand)
2. `-f file.vc` library files listed there (recursively)
3. the directory of the file being edited, and modules defined in the same
   buffer
4. `g:verilog_tooling_libdirs` / CLI `-y` dirs

Write the dirs in the Local Variables block at the bottom of the file:

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

### AUTO_TEMPLATE (EAI/EAP)

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
  `setq`; `vl-name`, `vl-cell-name`, `vl-width`, `vl-dir` are bound).
  In Python expressions, hyphenated names (`vl-width`) are automatically
  rewritten to underscore aliases (`vl_width`), since hyphens parse as
  subtraction. `vl-width`/`vl_width` is the *numeric* port width
  (`'4'` for `[0:3]`, `'1'` for a single bit, `'(1+(`a)-(`b))'` for
  parameterised ranges) — matching emacs `verilog-sig-width`.
- `/*AUTO_LISP(...)*/` and `/*AUTO_PYTHON(...)*/` prepare names that
  `@"..."` expressions can use — see
  [AUTO_PYTHON and AUTO_LISP](#auto_python-and-auto_lisp) below.

Without a template entry, a port connects to a same-named net (range
included); if the net is not declared yet, AW/AD declare it for you.
SystemVerilog `interface` ports (with modports) are supported:
`cpu_bus.master bus` connects as `.bus (bus.master)`.
`.*` instances expand as AUTOINST when star expansion is enabled.

Two EAI behaviors worth knowing:

- EAI is a **full reset** (emacs semantics): template-driven pins are
  marked `// Templated`; pins already connected **before** the marker
  line are kept. Use AIU/AIU1 to preserve hand-written connections in
  place.
- **Dimension notes are real information.** A connection of a
  multidimensional port carries the port's declared shape as a
  `net/*[D1][D2]*/` comment (`net/*[D1].[U1]*/` with unpacked dims):
  later passes read the note as the authoritative shape (note > port
  declaration > other evidence). Element/slice connections
  (`net[0][35:0]`) are self-describing and get no note. Never delete a
  note by hand; `kill` commands clear region contents but never touch
  connection notes, and AIU/AIU1 keep existing notes verbatim.

AUTOINSTPARAM (EAP) fills the `#(...)` parameter list in this priority:
an `AUTO_TEMPLATE` entry for the parameter; a same-named
parameter/localparam of the parent module (passed through symbolically);
the submodule's default value when visible in the parent; otherwise the
parameter is omitted.

### AUTO_PYTHON and AUTO_LISP

Template `@"..."` expressions often need more than the built-in
variables — a name transformation, a constant suffix, a string build.
Two comment blocks prepare Python names for them. Both are evaluated
in buffer order, only up to the instance being expanded, in a sandbox
with **no builtins** (no `import`, no file or network access); a
failing block aborts the command with the offending code quoted.

- `/*AUTO_LISP(...)*/` executes Python statements (assignments) whose
  names later `@"..."` expressions can read — the port of elisp's
  `AUTO_LISP` / `verilog-auto-lisp` preparation step, with Python syntax
  standing in for elisp `setq`.
- `/*AUTO_PYTHON( <code> )*/` runs full Python statements, typically
  `def`s; the defined names become callable from any following `@"..."`
  expression:

```verilog
/*AUTO_PYTHON(
def surround(sig):
    return "{" + sig + "," + sig + "}"
)*/
module top;
    /* sub AUTO_TEMPLATE (
        .din (@"surround(vl_name)"),
    ) */
    sub u_sub (/*autoinst*/);
endmodule
```

connects `.din` to `{din,din}`. Inside `@"..."` and AUTO_PYTHON code
alike, the template variables are available both in their elisp
spelling (`vl-name`, `vl-cell-name`, `vl-width`, `vl-dir`) and as
underscore aliases (`vl_name`, …).

Helper functions shared by many files can live in real `.py` files —
see the next section.

### `verilog-auto-python-file` — writing the Python helper file

Instead of pasting the same `/*AUTO_PYTHON*/` block into every buffer,
put the helpers in a real `.py` file and name it with a file-local
variable:

```verilog
// Local Variables:
// verilog-auto-python-file: "funcs.py"
// End:
```

(same quoted, whitespace-separated syntax as `verilog-library-files`;
parentheses optional, later files override earlier ones). With
`funcs.py` on the library path:

```python
def shout(sig):
    return sig.upper() + "_INT"
```

a template calls it exactly like an inline definition — the template
`.din (@"shout(vl_name)")` connects `din` to `DIN_INT`.

Resolution and precedence:

- files are searched in the `-y`/vc library directories, then the
  buffer's own directory (`~` and a leading `$VAR` expand);
- definitions are cached by file mtime — no stale helpers across runs;
- file-local files load first, then inline `/*AUTO_PYTHON*/` blocks;
  on a name clash the later definition wins (inline beats files);
- everything runs under the same no-builtins sandbox, so a helper file
  can define functions and constants but cannot touch the filesystem.

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

Disable the whole set with `let g:verilog_tooling_no_mappings = 1`.

## Insert abbreviations (optional, off by default)

`plugin/verilog_abbrev.vim` ports automatic.vim's global insert
abbreviations, scoped to `*.v`/`*.vh`/`*.sv`/`*.svh` buffers only:
`<=` → `` <= #`RD ``, `beg` → `begin`, `begni` (typo) → `begin`.
Enable in your vimrc with `let g:verilog_abbrev_enable = 1`.


## Command line

Everything the plugin does is also a command-line program — the Vim
front-end shells out to these same entry points (standard library
only). Point `PYTHONPATH` at the repo's `python/` directory (or the
installed `~/.vim/python`):

| Program | Commands |
|---|---|
| `python3 -m verilog_tooling.inst` | `aall eai eap ait aiu aiu1 kill aif apf adf af ainj` |
| `python3 -m verilog_tooling.arg` | `ar kill` (AUTOARG / KAR) |
| `python3 -m verilog_tooling.autodef` | `adt kill` (AD/ADT / KADT) |
| `python3 -m verilog_tooling.wire` | `aw ar kill-aw kill-ar` (AUTOWIRE / AUTOREG) |
| `python3 -m verilog_tooling.inout` | `aio ain aout ainout kill-ain kill-aout kill-ainout` |
| `python3 -m verilog_tooling.sense` | `asense areset kill-sense kill-reset` |
| `python3 -m verilog_tooling.misc` | `aascii alogic atieoff aunused aundef ainsertlisp ainsertlast` (+ `kill-*`) |
| `python3 -m verilog_tooling.xfer` | `ainoutmodule ainoutcomp ainoutin ainoutmodport ainoutparam aassignmodport aoutputevery areginput` (+ `kill-*`) |
| `python3 -m verilog_tooling.gen` | `am ame apm afm kill-para kill-fsm` (`am`/`ame` take `--line N`) |
| `python3 -m verilog_tooling.filehdr` | `template header` (`template -f new.v` creates the file) |
| `python3 -m verilog_tooling.inject` | `inject` (the AINJ marker pass on its own) |
| `python3 -m verilog_tooling.diffauto` | `diff` (unified diff to stdout, or `-o file`) |
| `python3 -m verilog_tooling.lint` | `lint` (unused-AUTO_TEMPLATE warnings to stdout) |

Common flags: `-i in.v -o out.v` (input/output files), `-y dir`
(library directory, repeatable), `-I name` (extra SystemVerilog
interface type name), `--ref_file f` (the real file behind a scratch
buffer, for Local-Variables relative paths). `verilog_tooling.inst`
adds:

| Flag | Effect |
|---|---|
| `--which N` | only the Nth `/*AUTOINST*/` marker (0-based) |
| `--line N` | only the instance at editor line N (AIT/AIU/AIU1 only) |
| `--sort` | sort EAI/EAP pins within each direction group |
| `--dot-name` | emit SystemVerilog `.name` shorthand connections |
| `--param-value` | substitute `#(...)` parameter values into port widths |
| `--star-expand` / `--star-save` | expand `.*` instances / keep the expansion tagged |
| `--date S` | override the INST_NEW/INST_DEL timestamp |

```bash
PYTHONPATH=python python3 -m verilog_tooling.inst eai -i top.v -o top.out.v -y rtl -y ip
```

## Layout

```
plugin/verilog_tooling.vim   Vim front-end (auto-loaded)
plugin/automatic.vim         header/waveform snippets (BPN/BP/BA, AddClk/AddSig/AddBus)
plugin/verilog_abbrev.vim    opt-in buffer-local iabbrevs for .v/.vh/.sv/.svh (off by default)
python/verilog_tooling/      the Python package (stdlib only)
doc/verilog_tooling.txt      vim help
install.sh                   one-click installer / env checker
```

Vim settings (`g:verilog_tooling_*`): see [Configuration](#configuration).

### Signal-dimension tracing (`VERILOG_TOOLING_TRACE`)

When a net comes out with the wrong width, trace where its dimensions
came from: set `VERILOG_TOOLING_TRACE` to a comma-separated list of
signal names (case-sensitive; `*` traces everything) and the
AUTOWIRE/AUTODEF decisions for those signals go to stderr, one line
per event (`first-driven`, `conn-dims`, `skip-declared`, `emit-decl`,
`update-width`/`update-dims`, `dt-exempt`, `multi-driver`, …):

```bash
VERILOG_TOOLING_TRACE=ram_rdata python3 -m verilog_tooling.inst aall -i a.v -o out.v -y .
# [vt-trace] ram_rdata: first-driven inst=u0_b module=b width=c0 pdims=['1:0', '35:0']
```

The trace is a debugging switch and is **not** suppressed by
`VERILOG_TOOLING_QUIET`.
