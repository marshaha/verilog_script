# verilog_script

Emacs `verilog-mode` AUTO expansions and the `automatic.vim` command set,
rewritten as a self-contained Vim plugin backed by a small Python package.

No pip dependencies (Python standard library only), no emacs required.

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

## Commands

| Command | What it does |
|---|---|
| `AALL` (`-a`) | full AUTO set: EAP → EAI → AW → AREG → AD → AR → AF |
| `EAI` / `EAP` | verilog-mode AUTOINST / AUTOINSTPARAM (regexp templates, `.*`, interfaces) |
| `AIT` / `AIU` / `AIU1` | instantiate template / update instances (keeps manual connections; AUTO_TEMPLATE wins where declared) |
| `AD` / `ADT` | regenerate `/*autodef*/` wire/reg/integer/genvar declarations |
| `AR` | regenerate `/*autoarg*/` header port lists |
| `AW` / `AREG` | AUTOWIRE / AUTOREG |
| `AF` | format: ports, wire/reg, parameter/localparam, instances |
| `AIF` `APF` `ADF` | individual format passes |
| `AM` `AME` `APM` `AFM` | module/parameter/FSM snippets |
| `AH` / `ATpl {file}` | file header / new file from template |
| `KI` `KAR` `KADT` | kill (collapse) the corresponding AUTO regions |
| `BPN` `BP` `BA` | module header / always-block snippets (from automatic.vim) |

`{count}` before EAI/AIT/AIU selects the Nth instance.

Every command reports what it did, e.g.
`[verilog_tooling] eai: line 515: 1005 -> 937 line(s)` (or `no changes`).

## AUTO_TEMPLATE / Local Variables

verilog-mode Local Variables are honored, including
`verilog-library-directories` (single- and multi-line forms),
`verilog-library-flags` (`-y dir`, `-f file.vc`), `verilog-inst-file`,
`verilog-typedef-regexp`, and `verilog-auto-inst-param-value`.
`-f` vc files support `-y`/`+incdir+`/`-I`/`-v`/`+libext+` entries.

AUTO_TEMPLATE supports exact/regexp entries, `@` instance numbering,
`[]` port-width expansion, and `@"..."` expressions in both a Python subset
and common elisp forms (`substring`, `downcase`, `concat`, `if`, `equal`,
arithmetic, `let`, `setq`, with `vl-name` / `vl-cell-name` / `vl-width` /
`vl-dir` bound).

## Layout

```
plugin/verilog_tooling.vim   Vim front-end (auto-loaded)
plugin/automatic.vim         header/waveform snippets (BPN/BP/BA, AddClk/AddSig/AddBus)
python/verilog_tooling/      the Python package (stdlib only)
doc/verilog_tooling.txt      vim help
install.sh                   one-click installer / env checker
```

Optional settings:

```vim
let g:verilog_tooling_python = '/usr/bin/python3'   " interpreter override
let g:verilog_tooling_libdirs = ['/path/to/rtl']    " extra -y search dirs
let g:verilog_tooling_eai_flags = ['--sort']        " extra EAI flags
```
