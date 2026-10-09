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
(`:1AIT` → the first one only). With no count, AIT/AIU/AIU1 process
the instance under the cursor — the front-end passes `--line` with
the cursor line — while EAI/EAP/KI process every instance. From the
command line, `--which N` (0-based marker index) or `--line N`
(1-based editor line, AIT/AIU/AIU1 only) selects a single instance.

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
expansion; `` `include`` edges to oddly-named include files are followed
too (basename index, no directory re-statting) — this read-through is on
by default, opt out with ``// verilog-auto-read-includes:nil`` at the
bottom of the file.  On a real project with 594 `-y`
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

Fills the `#(...)` parameter list from the submodule's parameter
declarations. With a `subp` defined as:

```verilog
module subp #(
    parameter DW = 8,
    parameter DEPTH = 4
) (
    input  wire          clk,
    output wire [DW-1:0] dout
);
endmodule
```

this:

```verilog
module top;
    subp #(/*AUTOINSTPARAM*/) u_subp (/*autoinst*/);
endmodule
```

becomes:

```verilog
module top;
    subp #(/*AUTOINSTPARAM*/
           // Parameters
           .DW                          (DW),
           .DEPTH                       (DEPTH)) u_subp (/*autoinst*/);
endmodule
```

See the priority rules in [AUTOINSTPARAM (EAP)](#autoinstparam-eap)
below.

### AIT — deprecated, delegates to EAI

`AIT` used to be automatic.vim AutoInst (kill the current connections
and regenerate identity connections). That lost hand-written connections
(`.clk (rx_clk)` became `.clk (clk)`), so the verb is deprecated: it now
prints a warning and delegates to the emacs (EAI) expansion, which keeps
every existing connection and only adds what is missing. The Vim
regeneration engine stays internal (AIU uses it to expand
`/*autoinst*/` stubs). Use `EAI` instead.

### AIU / AIU1 — minimal-diff updates

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

`AIU1` (keep every line in place):

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

`AIU` (same update, rewritten in module port order):

```verilog
module top;
    sub u_sub (/*autoinst*/
        .clk (rx_clk),
        .din (din),
    .dout                       (dout[7:0]                                  ), // output // INST_NEW 2026-10-08 18:49
    .cnt                        (cnt[3:0]                                   )  // output // INST_NEW 2026-10-08 18:49
//        .done(done) // INST_DEL: port done have deleted 2026-10-08 18:49
);
endmodule
```

The hand-written `.clk (rx_clk)` connection survives both; the
timestamp is the run time (`--date` overrides it on the command line).

### KI — collapse an instance

Deletes the generated pin list, leaving the `mod inst (/*autoinst*/);`
stub. The next EAI/AIT rebuilds from scratch:

```verilog
    sub u_sub (/*autoinst*/);
```

### AD / ADT — declare every undeclared signal

Regenerates the `/*autodef*/` region: io wires, flip-flop and
combinational registers, assign wires, instance-driven wires, and
for-loop variables (`integer`/`genvar`), with widths inferred from
drivers. Undriven outputs become `reg`. See [/*autodef*/
(AD/ADT)](#autodef-adadt) below for the full rules. `KADT` collapses the
region back to the marker.

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

### AR — header port list

Regenerates `/*autoarg*/` in the module header from the port
declarations: `//Inputs` / `//Outputs` / `//Inouts` sections, names
packed 4 spaces in and wrapped past column 40. Handles header-style
declarations (`input clk,`), several declarations on one line
(`input a, input b`), cross-line declarations, and unpacked dimensions
(`val[3:0]`). A misplaced marker (outside the header) is left untouched.
`KAR` collapses the list back to the marker.

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

(section order is Outputs, Inouts, Inputs — the emacs order).

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

AUTOSENSE:

```verilog
    always @(/*AUTOSENSE*/) begin
        q = a & b;
    end
```

becomes:

```verilog
    always @(/*AUTOSENSE*/a or b) begin
        q = a & b;
    end
```

AUTORESET:

```verilog
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            /*AUTORESET*/
        end else begin
            q   <= din;
            vld <= 1'b1;
        end
    end
```

becomes:

```verilog
        if (!rst_n) begin
            /*AUTORESET*/
            // Beginning of autoreset for uninitialized flops
            q <= 8'h0;
            vld <= 1'h0;
            // End of automatics
        end else begin
```

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

AIM example — an empty shell module plus the `sub` definition from the
AIU example gives:

```verilog
module shell;
/*AUTOINOUTMODULE("sub")*/
endmodule
```

becomes:

```verilog
module shell;
/*AUTOINOUTMODULE("sub")*/
// Beginning of automatic in/out/inouts (from specific module)
output [7:0]            dout;
output                  done;
input                   clk;
input [7:0]             din;
// End of automatics
endmodule
```

### AAMP — AUTOASSIGNMODPORT

`/*AUTOASSIGNMODPORT("If","mp-re","inst"[,"re"[,"prefix"]])*/` builds
`assign` statements wiring the modport signals to/from the interface
instance — for UVM verification modules.

With `bus_if.v` defining:

```verilog
interface bus_if;
    logic       req;
    logic       gnt;
    logic [7:0] data;
    modport master (output req, data, input gnt);
endinterface
```

this:

```verilog
module top;
    bus_if u_bus ();
    /*AUTOASSIGNMODPORT("bus_if", "master", "u_bus")*/
endmodule
```

becomes (modport outputs first, then inputs, each sorted by name; the
direction keyword carries across comma items, so `data` is an output
too):

```verilog
    /*AUTOASSIGNMODPORT("bus_if", "master", "u_bus")*/
    // Beginning of automatic assignments from modport
    assign data = u_bus.data;
    assign req = u_bus.req;
    assign u_bus.gnt = gnt;
    // End of automatics
```

### AOE / ARI — AUTOOUTPUTEVERY / AUTOREGINPUT

`/*AUTOOUTPUTEVERY[("re")]*/` declares every non-input signal an output
(keeps synthesis from optimizing signals away). `/*AUTOREGINPUT*/`
declares `reg` for undeclared nets feeding AUTOINST input pins
(`// To <inst> of <Mod>.v`), handy for top-level test shells.

AUTOOUTPUTEVERY acts on the module's declared signals:

```verilog
module top (
    input wire clk
);
    /*AUTOOUTPUTEVERY*/
    wire [7:0] dout_w;
    wire       done_w;
    sub u_sub (/*autoinst*/
        .clk  (clk),
        .din  (din),
        .dout (dout_w),
        .done (done_w)
    );
endmodule
```

becomes:

```verilog
    /*AUTOOUTPUTEVERY*/
    // Beginning of automatic outputs (every signal)
    output              done_w;
    output [7:0]        dout_w;
    // End of automatics
```

AUTOREGINPUT on the same instance:

```verilog
    /*AUTOREGINPUT*/
    // Beginning of automatic reg inputs (for undeclared instantiated-module inputs)
    reg                 clk;                    // To u_sub of sub.v
    reg [7:0]           din;                    // To u_sub of sub.v
    // End of automatics
```

### AASC — AUTOASCIIENUM

`/*AUTOASCIIENUM("sig", "ascii_sig"[,"prefix"[,"onehot"]])*/` builds an
ASCII decode register for an enum state vector: parameters tagged
`// auto enum <name>` (or `synopsys enum`) define the states, the signal
tagged `/* auto state_vector <sig> */` selects the vector. Emits
`reg [8*N-1:0] ascii_sig; // Decode of sig` plus the `always @(sig)`
case decoder (`"%Err"` default).

The canonical shape (note the enum tag on the state variable itself,
which links it to the parameter group):

```verilog
module fsm;
    //== State enumeration
    parameter [2:0] // auto enum state_info
        SM_IDLE  = 3'b001,
        SM_SEND  = 3'b010,
        SM_WAIT1 = 3'b100;
    //== State variables
    reg [2:0] /* auto enum state_info */
        state_r; /* auto state_vector state_r */
    /*AUTOASCIIENUM("state_r", "state_ascii_r", "SM_")*/
endmodule
```

becomes:

```verilog
    /*AUTOASCIIENUM("state_r", "state_ascii_r", "SM_")*/
    // Beginning of automatic ASCII enum decoding
    reg [39:0]          state_ascii_r;          // Decode of state_r
    always @(state_r) begin
       case ({state_r})
         SM_IDLE:  state_ascii_r = "idle ";
         SM_SEND:  state_ascii_r = "send ";
         SM_WAIT1: state_ascii_r = "wait1";
         default:  state_ascii_r = "%Erro";
       endcase
    end
    // End of automatics
```

(the `"SM_"` prefix is stripped from the decoded names, which are
space-padded to the widest state name).

### ALGC — AUTOLOGIC

`/*AUTOLOGIC*/` is AUTOWIRE declaring `logic` instead of `wire`. A file-local
`// verilog-auto-wire-type: "logic"` switches `/*AUTOWIRE*/` the same way.

```verilog
    /*AUTOLOGIC*/
    // Beginning of automatic wires (for undeclared instantiated-module outputs)
    logic                                   done_w; // From u_sub of sub.v
    logic        [7:0]                      dout_w; // From u_sub of sub.v
    // End of automatics
```

### ATIE — AUTOTIEOFF

`/*AUTOTIEOFF*/` ties every unterminated module output to deasserted:
`wire [w:0] o = w'h0;` (`~w'h0` for active-low names, `w'sh0` for signed).
Outputs already declared, driven by AUTOINST, or matching
`verilog-auto-tieoff-ignore-regexp` are skipped. The classic stub-module
companion to `AUNU`.

```verilog
module stub (
    output wire [7:0] o_data,
    output wire       o_vld_n
);
    /*AUTOTIEOFF*/
endmodule
// Local Variables:
// verilog-active-low-regexp: "_n$"
// End:
```

becomes (the `_n` output is tied to 1 as `~1'h0`, per the active-low
variable):

```verilog
    /*AUTOTIEOFF*/
    // Beginning of automatic tieoffs (for this module's unterminated outputs)
    wire [7:0]          o_data                  = 8'h0;
    wire                o_vld_n                 = ~1'h0;
    // End of automatics
```

### AUNU — AUTOUNUSED

`/*AUTOUNUSED*/` expands inline to the comma-separated list of unused
input/inout signals — designed for
`wire _unused_ok = &{1'b0, /*AUTOUNUSED*/ 1'b0};` so one pragma silences
all unused warnings. `verilog-auto-unused-ignore-regexp` excludes names.

"Unused" means *not connected to any instance input/inout pin*
(connections inside `{...}`/`(...)` count as used); being read by an
`assign` does not rescue a signal. In this module `spare_a`/`spare_b`
feed nothing:

```verilog
module top (
    input  wire       clk,
    input  wire [7:0] din,
    input  wire       spare_a,
    input  wire       spare_b,
    output wire [7:0] dout
);
    sub u_sub (/*autoinst*/
        .clk (clk),
        .din (din),
        .dout(dout));
    wire unused_ok = &{1'b0,
                       /*AUTOUNUSED*/
                       1'b0};
endmodule
```

becomes:

```verilog
    wire unused_ok = &{1'b0,
                       /*AUTOUNUSED*/
                       // Beginning of automatic unused inputs
                       spare_a,
                       spare_b,
                       // End of automatics
                       1'b0};
```

### AUND — AUTOUNDEF

`/*AUTOUNDEF[("re")]*/` emits `` `undef `` for every `` `define `` seen
since the previous AUTOUNDEF (already-undef'd names are skipped, so
`` `ifdef NEVER `` guards work), sorted, optionally regexp-filtered —
keeps file-local defines out of the global namespace.

```verilog
`define FOO 8
`define BAR 16
module top;
    /*AUTOUNDEF*/
endmodule
```

becomes:

```verilog
    /*AUTOUNDEF*/
    // Beginning of automatic undefs
`undef BAR
`undef FOO
    // End of automatics
```

### AIL / AILL — AUTOINSERTLISP / AUTOINSERTLAST

`/*AUTOINSERTLISP(!command args)*/` runs the shell command and inserts its
stdout into a `// Beginning of automatic insert lisp` region, before (AIL)
or after (AILL) all other AUTOs. Emacs evaluates elisp here; this port runs
shell instead — the documented difference (elisp `defun`s are not
supported, mirroring the AUTO_TEMPLATE `@"..."` subset rule).

```verilog
module top;
    /*AUTOINSERTLISP(!echo // inserted by shell)*/
endmodule
```

becomes:

```verilog
module top;
    /*AUTOINSERTLISP(!echo // inserted by shell)*/
    // Beginning of automatic insert lisp
// inserted by shell
    // End of automatics
endmodule
```

### AINJ — inject AUTOs into legacy code

Inserts `/*AUTOARG*/` into module headers, `/*AS*/` into always blocks
whose hand-written sensitivity list already matches, and `/*AUTOINST*/`
into pin lists (deleting `.x(x)` identity pins), then runs the full AALL
pipeline — the `verilog-inject-auto` workflow for bringing old files
under AUTO control.

```verilog
module top (clk, din, dout, done);
    input clk;
    input [7:0] din;
    output [7:0] dout;
    output done;
    reg [7:0] r;
    always @(clk or din) r = din;
    sub u_sub (.clk(clk), .din(din), .dout(dout), .done(done));
endmodule
```

becomes:

```verilog
module top (clk, din, dout, done/*AUTOARG*/
    //Outputs
    dout, done,

    //Inputs
    clk, din
);
input                                clk;
input[7:0]                           din;
output[7:0]                          dout;
output                               done;
reg[7:0]                             r;
    always @(clk or din) r = din;
    sub u_sub (
               /*AUTOINST*/
               // Outputs
        .dout   (dout[7:0]                                                  ),
        .done   (done                                                       ),
               // Inputs
        .clk    (clk                                                        ),
        .din    (din[7:0]                                                   )
);
endmodule
```

(The `/*AS*/` marker is only injected when the existing hand-written
sensitivity list already agrees with the block's reads — otherwise the
always block is left alone for you to fix first.)

### ADIF — diff AUTOs

Expands AUTOs on a copy and shows the unified diff in a preview window
(whitespace-insensitive detection, like `verilog-diff-auto`). Empty output
means the buffer is fully expanded — suitable for a lint/regression check.

A stale `.stale(stale)` pin plus a pending re-expansion shows as:

```diff
--- current
+++ auto-expanded
@@ -1,9 +1,10 @@
 module top;
     sub u_sub (/*autoinst*/
-        .clk (clk),
-        .din (din),
-        .dout(dout),
-        .done(done),
-        .stale(stale)
-    );
+               // Outputs
+        .dout   (dout[7:0]                                                  ),
+        .done   (done                                                       ),
+               // Inputs
+        .clk    (clk                                                        ),
+        .din    (din[7:0]                                                   )
+);
 endmodule
```

### ATLINT — unused AUTO_TEMPLATE lines

Runs the EAI/EAP expansion with hit-tracking and lists template entries
never consumed by any instance in the quickfix window
(`verilog-auto-template-warn-unused`).

For this buffer, `.nosuch` matches no port of `sub`, so it is reported;
`.din` is consumed and stays silent:

```verilog
module top;
    /* sub AUTO_TEMPLATE (
        .din (data_in),
        .nosuch (nosuch),
    ) */
    sub u_sub (/*autoinst*/);
endmodule
```

```
top.v:2: AUTO_TEMPLATE line unused: ".nosuch (nosuch)"
```

### AF family — alignment

Buffer-local formatting (no module files needed):

- `APF` aligns port declarations (`input`/`output`/`inout`),
- `ADF` aligns `wire`/`reg`/`logic` declarations and
  `parameter`/`localparam` (`=` signs aligned),
- `AIF` aligns instance port connections,
- `AF` runs all three.

Idempotent; reports `no changes` when already aligned.

```verilog
module top(input clk, input [7:0] din, output [7:0] dout);
parameter W = 8;
localparam DEPTH = 16;
wire [7:0] a;
wire b;
sub u_sub(
.clk(clk),
.din(a),
.dout (dout),
.done(b));
endmodule
```

after `AF`:

```verilog
module top(input clk, input [7:0] din, output [7:0] dout);
parameter   W     = 8;
localparam  DEPTH = 16;
wire[7:0]                           a;
wire                                b;
sub u_sub(
        .clk    (clk                                                        ),
        .din    (a                                                          ),
        .dout   (dout                                                       ),
        .done   (b                                                          )
);
endmodule
```

Hand-written `#(...)` parameter overrides in an instance header are
aligned exactly like pin connections — only the `)) inst (` closer
line is never folded into the last override.

### AM / AME — instance stubs

Turns the word under the cursor into an instance stub on that line.
The instance index counts the module's previous instances in the
buffer. `fifo` under the cursor becomes, with `AM` (automatic.vim
style):

```verilog
fifo  u0_fifo(/*autoinst*/);
```

and with `AME` (emacs-flavored, ready for EAP + a template):

```verilog
/* fifo  auto_template (
  ); */
fifo #(/*autoinstparam*/)   u0_fifo(/*autoinst*/);
```

### APM / AFM — parameter / FSM skeletons

`/*autopara*/ (A, B=2, C)` expands into aligned `parameter`
declarations:

```verilog
    /*autopara*/ (A, B=2, C)
// Define parameter here
parameter A = 2'd0;
parameter B = 2'd2;
parameter C = 2'd3;
// End of automatic parameter
```

`/*autofsm*/ (IDLE,RUN,DONE) state nstate` expands into state
localparams plus the two-always-block FSM skeleton (state register +
next-state logic), with the state width derived from the state count:

```verilog
    /*autofsm*/ (IDLE,RUN,DONE) state nstate
// Define fsm here
// Define FSM parameter here
localparam IDLE = 2'd0;
localparam RUN  = 2'd1;
localparam DONE = 2'd2;
// End of automatic parameter for FSM
always @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin
        state[1:0] <= #`RD IDLE;
    end else begin
        state[1:0] <= #`RD nstate[1:0];
    end
end
always @(*) begin
    nstate[1:0] = state[1:0];
    case(state[1:0])
        IDLE: begin
        end
        RUN: begin
        end
        DONE: begin
        end
        default: begin
        end
    endcase
end
// End of automatic fsm
```

### AH / ATpl — file header & new file

`AH` prepends the file header comment block (`// +FHDR`):

```verilog
// +FHDR----------------------------------------------------------------------
//                 Copyright (c) 2026 .
//                     ALL RIGHTS RESERVED
//  This source file is the property of   Technology Co., Ltd. and
//  may not be copied or distributed in any isomorphic form without the prior
//  written consent of  Technology Co., Ltd.
// ---------------------------------------------------------------------------
// Filename      : hdr.v
// Author        :
// Created On    : 2026-10-08 18:47
// Last Modified :
// ---------------------------------------------------------------------------
// Description:
//
//
// -FHDR----------------------------------------------------------------------
```

`ATpl foo.v` creates a new file from the project skeleton (after the
same `+FHDR` block) and opens it — a `_tb`/`tb` suffix produces the
testbench skeleton:

```verilog
//`timescale 1ns/1ps

module foo_tb(/*autoarg*/);
/*autoreginput*/


reg                                     clk;
reg                                     rst_n;
/*autodef off*/
initial begin
    clk = 1'b0;
    forever #10 clk = ~clk;
end
initial begin
    rst_n = 1'b0;
    #52 rst_n = 1'b1;
end
initial begin
    $fsdbDumpfile("main.fsdb") ;
    $fsdbDumpvars(0,foo_tb,"+mda");
end
initial begin
    #1000;
    $finish;
end
/*autodef on*/

//{{{
/*autodef*/
/*autowire*/
/*autoreg*/
//}}}

//inst u_inst(/*autoinst*/);

// Local Variables:
// verilog-auto-inst-param-value:t
// verilog-library-flags:("-y  <dir-of-file>" )
// verilog-library-directories:("<dir-of-file>" )
// End:
```

(the two library paths are written with the new file's own directory).
New empty `.v`/`.sv` buffers get the skeleton automatically
(BufNewFile).

### BPN / BP / BA — always-block snippets

Insert an always skeleton at the cursor. `BPN`:

```verilog
always @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin

    end else if() begin
    end else begin
    end
end
```

`BP`:

```verilog
always @(posedge clk) begin
    if() begin

    end else begin
    end
end
```

`BA`:

```verilog
always @(*) begin

end
```

(the cursor is left in the empty first branch of each skeleton).

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

Other supported verilog-mode file-locals (all default to the emacs value
when absent):

| Variable | Default | Effect |
|---|---|---|
| `verilog-auto-inst-vector` | `t` | bus subscripts on default AUTOINST connections; `nil` skips the subscript when the parent declares the net with a matching width, `unsigned` subscripts only unsigned ports |
| `verilog-auto-inst-sort` | `nil` | sorts AUTOINST pins within each direction group |
| `verilog-auto-inst-dot-name` | `nil` | SystemVerilog `.name` shorthand when the connection equals the port name |
| `verilog-auto-simplify-expressions` | `t` | `nil` keeps range expressions verbatim instead of folding constants |
| `verilog-auto-inst-template-required` | `nil` | non-nil omits ports with no template entry from AUTOINST |
| `verilog-auto-arg-sort` | `nil` | sorts AUTOARG port names instead of declaration order |
| `verilog-auto-arg-format` | `packed` | `single` puts one AUTOARG port per line |
| `verilog-auto-declare-nettype` | `nil` | io declarations with no data type get `<direction> <nettype>` (for `` `default_nettype none``) |

Note: AUTOARG sections follow the emacs order — Outputs, Inouts, Inputs.

## AUTO_PYTHON and AUTO_LISP

Template `@"..."` expressions often need more than the built-in
variables — a name transformation, a constant suffix, a string build.
Two comment blocks prepare Python names for them. Both are evaluated
in buffer order, only up to the instance being expanded, in a sandbox
with **no builtins** (no `import`, no file or network access); a
failing block aborts the command with the offending code quoted.

### AUTO_LISP — plain bindings

`/*AUTO_LISP(...)*/` executes Python statements (assignments) whose
names later `@"..."` expressions can read. It is the port of elisp's
`AUTO_LISP` / `verilog-auto-lisp` preparation step, with Python syntax
standing in for elisp `setq`:

```verilog
/*AUTO_LISP(SFX = "_r")*/
module top;
    /* sub AUTO_TEMPLATE (
        .din (@"vl_name + SFX"),
    ) */
    sub u_sub (/*autoinst*/);
endmodule
```

After `EAI`, the `din` pin is connected to `din_r`:

```verilog
               .din                     (din_r));                // Templated
```

### AUTO_PYTHON — defining functions

`/*AUTO_PYTHON( <code> )*/` runs full Python statements, typically
`def`s, at module level. The defined names become callable from any
following `@"..."` expression — the Python-native alternative to
defining elisp helpers for a template:

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

After `EAI`:

```verilog
    sub u_sub (/*autoinst*/
               // Outputs
               .dout                    (dout[7:0]),
               .done                    (done),
               // Inputs
               .clk                     (clk),
               .din                     ({din,din}));            // Templated
```

Inside `@"..."` and inside AUTO_PYTHON code alike, the template
variables are available both in their elisp spelling (`vl-name`,
`vl-cell-name`, `vl-width`, `vl-dir` — hyphenated names in `@"..."`
strings are rewritten automatically) and as underscore aliases
(`vl_name`, `vl_cell_name`, `vl_width`, `vl_dir`), because hyphens are
not legal Python identifiers.

### `verilog-auto-python-file` — shared Python files

Helper functions shared by many files can live in real `.py` files,
loaded via a file-local variable (same quoted, whitespace-separated
syntax as `verilog-library-files`; parentheses optional, later files
override earlier ones):

```verilog
// Local Variables:
// verilog-auto-python-file: "funcs.py"
// End:
```

with `funcs.py` on the library path:

```python
def shout(sig):
    return sig.upper() + "_INT"
```

and the template `.din (@"shout(vl_name)")`, `EAI` connects:

```verilog
               .din                     (DIN_INT));              // Templated
```

Resolution and precedence:

- files are searched in the `-y`/vc library directories, then the
  buffer's own directory (`~` and a leading `$VAR` expand);
- definitions are cached by file mtime — no stale helpers across runs;
- file-local files load first, then inline `/*AUTO_PYTHON*/` blocks;
  on a name clash the later definition wins (inline beats files);
- everything runs under the same no-builtins sandbox, so a helper file
  can define functions and constants but cannot touch the filesystem.

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

```verilog
    /*AUTOWIRE*/
    sub u_sub (/*autoinst*/
        .clk  (clk),
        .din  (din),
        .dout (dout_w),
        .done (done_w)
    );
```

becomes:

```verilog
    /*AUTOWIRE*/
    // Beginning of automatic wires (for undeclared instantiated-module outputs)
    wire                                    done_w; // From u_sub of sub.v
    wire         [7:0]                      dout_w; // From u_sub of sub.v
    // End of automatics
```

and `/*AUTOREG*/` in a 1995-style module:

```verilog
module top (q, vld);
    output [7:0] q;
    output vld;
    /*AUTOREG*/
endmodule
```

becomes:

```verilog
    /*AUTOREG*/
    // Beginning of automatic regs (for this module's undeclared outputs)
    reg          [7:0]                      q;
    reg                                     vld;
    // End of automatics
```

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

```verilog
module wrap;
    /*AUTOINPUT*/
    /*AUTOOUTPUT*/
    /*AUTOINOUT*/
    sub u_sub (/*autoinst*/
        .clk  (clk),
        .din  (din),
        .dout (dout),
        .done (done)
    );
endmodule
```

becomes:

```verilog
module wrap;
    /*AUTOINPUT*/
    // Beginning of automatic inputs (from unused autoinst inputs)
    input                                   clk; // To u_sub of sub.v
    input        [7:0]                      din; // To u_sub of sub.v
    // End of automatics
    /*AUTOOUTPUT*/
    // Beginning of automatic outputs (from unused autoinst outputs)
    output                                  done; // From u_sub of sub.v
    output       [7:0]                      dout; // From u_sub of sub.v
    // End of automatics
    /*AUTOINOUT*/
    sub u_sub (/*autoinst*/
        .clk  (clk),
        .din  (din),
        .dout (dout),
        .done (done)
    );
endmodule
```

AIO and `/*autoarg*/`: with `/*autoarg*/` in the header, put the
AUTOINPUT/AUTOOUTPUT markers in the **body** — autoarg then packs the
generated port names into the header (the canonical verilog-mode layout).
When the AIO markers are in the header themselves, autoarg leaves that
header alone (a name list would duplicate the full declarations).

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

### Signal-dimension tracing (`VERILOG_TOOLING_TRACE`)

When a net comes out with the wrong width, trace where its dimensions came
from: set `VERILOG_TOOLING_TRACE` to a comma-separated list of signal names
(case-sensitive; `*` traces everything) and the AUTOWIRE/AUTODEF decisions
for those signals go to stderr, one line per event:

```bash
VERILOG_TOOLING_TRACE=ram_rdata vim a.v        # then :AALL
# or terminal/batch:
VERILOG_TOOLING_TRACE=ram_rdata python3 -m verilog_tooling.inst aall -i a.v -o out.v -y .
```

Sample output:

```
[vt-trace] ram_rdata: first-driven inst=u0_b module=b width=c0 pdims=['1:0', '35:0']
[vt-trace] ram_rdata: skip-declared decl_width=35 decl_pdims=[]
[vt-trace] ram_rdata: update-dims before=wire [35:0] ram_rdata; after=wire [1:0][35:0] ram_rdata;
```

Events use fixed verbs — `first-driven` (first recorded driver, with
inst/module/width/pdims), `conn-dims` (dims extracted from a connection
note / port packed dims), `skip-declared` (already declared, with the
existing declaration's dims), `emit-decl` (final declared width/dims),
`extend-from-side` / `merge-dims` (assignment-side width/dim evidence and
unpacked-dim merges), `update-width` / `update-dims` (a hand-written
declaration corrected in place), `dt-exempt` (a `//DT` waiver blocked an
update), `absorb` / `waive-keep` (kill-waiver orphan handling),
`multi-driver`. Unset/empty variable costs one cached environment read;
the trace is a debugging switch and is **not** suppressed by
`VERILOG_TOOLING_QUIET`.

`g:verilog_tooling_interfaces` lists SystemVerilog interface type names the
library scan cannot find (the file is not under any `-y` dir). Ports like
`axera_apb_interface.master apb` then parse as interface ports for
EAI/AIT/AIU — EAI puts them in the `// Interfaces` section with a
`name.modport` connection — and are never mistaken for wires by
AW/AREG/AD/AIO.
