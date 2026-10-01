" verilog_tooling.vim — run the Python verilog_tooling CLI on the current
" buffer and refresh the buffer with the result.
"
" The current buffer content (including unsaved edits) is written to a temp
" file, the Python command transforms it, and the result is loaded back with
" setline()/delete — so undo history, marks, folds and cursor are preserved,
" and the file on disk is never touched.
"
" Install (self-contained, shareable ~/.vim):
"   ~/.vim/plugin/verilog_tooling.vim   this file (auto-loaded by Vim)
"   ~/.vim/python/verilog_tooling/      the Python package (stdlib only)
" Uses the system python3 by default — no venv, no pip install needed.
" Optional globals:
"   g:verilog_tooling_python   — python executable (default: python3)
"   g:verilog_tooling_libdirs  — extra -y module library dirs (list)
"   g:verilog_tooling_eai_flags — extra flags for :EAI (e.g. ['--sort'])
"   g:verilog_tooling_interfaces — user-known SV interface type names (list,
"                             e.g. ['axera_apb_interface'] — supplements the
"                             ones found by scanning the -y dirs)
"
" Commands (same names as the automatic.vim bindings):
"   AALL                      full AUTO expansion (EAP+EAI+AIO+AW+AREG+AD+AR+AF,
"                             emacs verilog-batch-auto order)
"   AIT AIU AIU1 KI           instance commands (automatic.vim AIT/AIU/AIU1 + kill)
"   EAI EAP                   verilog-mode AUTOINST / AUTOINSTPARAM
"   AIF APF ADF AF            format family (buffer-local)
"   AR KAR                    automatic.vim AutoArg / KillAutoArg
"   AD ADT KADT               automatic.vim AutoDef(T) / KillAutoDefT
"   AH ATpl                   file header (// +FHDR) / new-file template
"   AM AME                    instance stub generators (cursor line)
"   APM AFM                   automatic.vim AutoPara / AutoFsm
"   AW AREG                   verilog-mode AUTOWIRE / AUTOREG
"   AIO                       verilog-mode AUTOOUTPUT + AUTOINPUT + AUTOINOUT
" A count selects the Nth /*autoinst*/ instance (0-based): :1AIT → first only.
"
" Default leader mappings are installed at the bottom of this file (only
" when the lhs is still unmapped — your own mappings always win):
"   <leader>a   :AALL     <leader>ad  :AD     <leader>adt :ADT
"   <leader>ait :AIT      <leader>aiu :AIU    <leader>aiu1:AIU1
"   <leader>ar  :AR       <leader>aw  :AW     <leader>arg :AREG
"   <leader>aif :AIF      <leader>adf :ADF    <leader>apf :APF  <leader>af :AF
"   <leader>am  :AM       <leader>ame :AME    <leader>d   :KI
"   <leader>eai :EAI      <leader>eap :EAP    <leader>aio :AIO
" Disable the whole set with:  let g:verilog_tooling_no_mappings = 1

if exists('g:loaded_verilog_tooling')
    finish
endif
let g:loaded_verilog_tooling = 1

" Self-contained layout (shareable ~/.vim):
"   ~/.vim/plugin/verilog_tooling.vim  (this file)
"   ~/.vim/python/verilog_tooling/     (the Python package, stdlib only)
" tool_root = the .vim dir (two levels up from plugin/), python/ holds the
" package. Uses the system python3 by default — no venv needed.
let s:tool_root = expand('<sfile>:p:h:h')
let s:py_path = s:tool_root . '/python'

function! s:Python() abort
    if exists('g:verilog_tooling_python')
        return g:verilog_tooling_python
    endif
    let l:venv = s:tool_root . '/.venv/bin/python3'
    return filereadable(l:venv) ? l:venv : 'python3'
endfunction

" One-time interpreter sanity check: the package needs Python >= 3.8.
let s:py_checked = 0
function! s:CheckPython() abort
    if s:py_checked
        return 1
    endif
    let s:py_checked = 1
    let l:py = s:Python()
    if !executable(l:py)
        echohl ErrorMsg
        echom '[verilog_tooling] python3 not found: ' . l:py
              \ . ' (set g:verilog_tooling_python to a valid interpreter)'
        echohl None
        return 0
    endif
    call system(l:py . ' -c "import sys; sys.exit(sys.version_info < (3, 8))"')
    if v:shell_error != 0
        echohl ErrorMsg
        echom '[verilog_tooling] Python >= 3.8 required by ' . l:py
        echohl None
        return 0
    endif
    return 1
endfunction

function! s:LibdirArgs() abort
    let l:args = []
    for l:d in (exists('g:verilog_tooling_libdirs') ? g:verilog_tooling_libdirs : [])
        call extend(l:args, ['-y', l:d])
    endfor
    return l:args
endfunction

" User-known SystemVerilog interface type names (they supplement the ones
" found by scanning the -y dirs), e.g. in vimrc:
"   let g:verilog_tooling_interfaces = ['axera_apb_interface', 'axera_axi_interface']
" Interface-typed ports (axera_apb_interface.master apb) then parse as
" interface ports for EAI/AIT/AIU/AW/AIO/AD.
function! s:InterfaceArgs() abort
    let l:args = []
    for l:i in (exists('g:verilog_tooling_interfaces') ? g:verilog_tooling_interfaces : [])
        call extend(l:args, ['--interface', l:i])
    endfor
    return l:args
endfunction

" Apply the transformed lines to buffer a:bufnr with a minimal-diff update
" (undo history, marks, folds and cursor survive); reports what changed.
function! s:ApplyResult(bufnr, new, cmd) abort
    let l:cur = bufnr('%')
    if l:cur != a:bufnr
        " the update helpers (setline/deletebufline/append) target the
        " current buffer — bail out rather than touch the wrong one
        echohl ErrorMsg
        echom printf('[verilog_tooling] %s: finished, but you switched buffers — result left unapplied (re-run the command)', a:cmd)
        echohl None
        return
    endif
    let l:old = getline(1, '$')
    if a:new ==# l:old
        echom '[verilog_tooling] ' . a:cmd . ': no changes'
        return
    endif
    let l:view = winsaveview()
    " minimal-diff update: only replace the lines that actually differ,
    " so Vim redraws less and marks/folds outside the change survive
    let l:min = min([len(l:old), len(a:new)])
    let l:i = 0
    while l:i < l:min && l:old[l:i] ==# a:new[l:i]
        let l:i += 1
    endwhile
    let l:j = 0
    while l:j < l:min - l:i && l:old[len(l:old) - 1 - l:j] ==# a:new[len(a:new) - 1 - l:j]
        let l:j += 1
    endwhile
    let l:mid = a:new[l:i : len(a:new) - l:j - 1]
    if l:i == 0 && l:j == 0
        " no common head/tail: plain whole-buffer replace (deleting all
        " lines would leave a stray empty line behind)
        call setline(1, l:mid)
        if line('$') > len(l:mid)
            call deletebufline('', len(l:mid) + 1, '$')
        endif
    else
        " deletebufline()/append() instead of an ex :delete range: a
        " 'N,Mdelete _' via :execute was observed to delete ~10 lines
        " beyond M on large buffers, silently corrupting the buffer
        if len(l:old) - l:j >= l:i + 1
            call deletebufline('', l:i + 1, len(l:old) - l:j)
        endif
        if !empty(l:mid)
            call append(l:i, l:mid)
        endif
    endif
    call winrestview(l:view)
    setlocal modified
    let l:deleted = len(l:old) - l:j - l:i
    echom printf('[verilog_tooling] %s: line %d: %d -> %d line(s)',
          \ a:cmd, l:i + 1, l:deleted, len(l:mid))
endfunction

" stderr lines of the async job: python progress logs and tracebacks.
" Show them live in :messages so a big top visibly makes progress.
" Long lines (module lists) are truncated — an overlong echom triggers a
" hit-enter prompt that would stall a headless run.
function! s:OnLog(cmd, channel, msg) abort
    if a:msg !=# ''
        echom strpart(a:msg, 0, 200) . (len(a:msg) > 200 ? ' …' : '')
    endif
endfunction

function! s:OnExit(cmd, bufnr, tick, infile, outfile, job, code) abort
    if getbufvar(a:bufnr, 'verilog_tooling_running')
        call setbufvar(a:bufnr, 'verilog_tooling_running', 0)
    endif
    if a:code != 0
        echohl ErrorMsg
        echom printf('[verilog_tooling] %s: failed (exit %d) — see :messages', a:cmd, a:code)
        echohl None
    elseif getbufvar(a:bufnr, 'changedtick') != a:tick
        echohl WarningMsg
        echom printf('[verilog_tooling] %s: buffer was edited while running — result NOT applied (re-run)', a:cmd)
        echohl None
    elseif filereadable(a:outfile)
        call s:ApplyResult(a:bufnr, readfile(a:outfile), a:cmd)
    endif
    call delete(a:infile)
    call delete(a:outfile)
endfunction

" Run python -m {mod} {cmd} [extra] on the current buffer, ASYNCHRONOUSLY:
" python progress logs stream into :messages while it works (proof it is
" alive on big tops), the buffer is refreshed on completion.  Re-entry on
" the same buffer is blocked; edits made while it runs abort the apply.
" a:which >= 0 passes --which (Nth /*autoinst*/ instance).
function! s:Run(mod, cmd, extra, which) abort
    if !s:CheckPython()
        return
    endif
    if get(b:, 'verilog_tooling_running')
        echohl WarningMsg
        echom '[verilog_tooling] a command is already running on this buffer'
        echohl None
        return
    endif
    let b:verilog_tooling_running = 1
    let l:tick = b:changedtick
    let l:in = tempname() . '.v'
    let l:out = tempname() . '.v'
    call writefile(getline(1, '$'), l:in)

    " The buffer content goes to a temp file, but Local-Variables relative
    " paths must resolve against the REAL file's directory, not the temp dir.
    let l:argv = [s:Python(), '-m', a:mod, a:cmd, '-i', l:in, '-o', l:out,
          \ '--ref_file', expand('%:p')]
    if a:which >= 0
        call extend(l:argv, ['--which', string(a:which)])
    endif
    call extend(l:argv, a:extra)
    call extend(l:argv, s:LibdirArgs())

    echom printf('[verilog_tooling] %s: running ...', a:cmd)
    let l:job = job_start(l:argv, {
          \ 'env': {'PYTHONPATH': s:py_path . (empty($PYTHONPATH) ? '' : ':' . $PYTHONPATH)},
          \ 'err_cb': function('s:OnLog', [a:cmd]),
          \ 'exit_cb': function('s:OnExit', [a:cmd, bufnr('%'), l:tick, l:in, l:out]),
          \ })
    if job_status(l:job) ==# 'fail'
        let b:verilog_tooling_running = 0
        echohl ErrorMsg | echom '[verilog_tooling] failed to start python job' | echohl None
    endif
endfunction

function! s:InstCmd(cmd, extra) abort
    " v:count1 is 1 when no count given; map to -1 = all instances.
    call s:Run('verilog_tooling.inst', a:cmd, a:extra, v:count1 - 1)
endfunction

" Run the full AUTO expansion set in emacs verilog-batch-auto order:
" EAP (AUTOINSTPARAM) -> EAI (AUTOINST) -> AW (AUTOWIRE) -> AREG (AUTOREG)
" -> AD (autodef) -> AR (autoarg) -> AF (all format).
" One python process for the whole pipeline (module files are resolved and
" read once); g:verilog_tooling_eai_flags (e.g. --sort) is passed through.
function! s:AutoAll() abort
    call s:Run('verilog_tooling.inst', 'aall', get(g:, 'verilog_tooling_eai_flags', []) + s:InterfaceArgs(), -1)
endfunction

command! -nargs=0 AALL call s:AutoAll()

command! -count=0 AIT  call s:InstCmd('ait', s:InterfaceArgs())
command! -count=0 AIU  call s:InstCmd('aiu', s:InterfaceArgs())
command! -count=0 AIU1 call s:InstCmd('aiu1', s:InterfaceArgs())
command! -count=0 KI   call s:InstCmd('kill', [])
command! -count=0 EAI  call s:InstCmd('eai', get(g:, 'verilog_tooling_eai_flags', []) + s:InterfaceArgs())
command! -count=0 EAP  call s:InstCmd('eap', s:InterfaceArgs())

command! -nargs=0 AIF  call s:Run('verilog_tooling.inst', 'aif', [], -1)
command! -nargs=0 APF  call s:Run('verilog_tooling.inst', 'apf', [], -1)
command! -nargs=0 ADF  call s:Run('verilog_tooling.inst', 'adf', [], -1)
command! -nargs=0 AF   call s:Run('verilog_tooling.inst', 'af', [], -1)

command! -nargs=0 AR   call s:Run('verilog_tooling.arg', 'ar', [], -1)
command! -nargs=0 KAR  call s:Run('verilog_tooling.arg', 'kill', [], -1)
command! -nargs=0 AD   call s:Run('verilog_tooling.autodef', 'adt', s:InterfaceArgs(), -1)
command! -nargs=0 ADT  call s:Run('verilog_tooling.autodef', 'adt', s:InterfaceArgs(), -1)
command! -nargs=0 KADT call s:Run('verilog_tooling.autodef', 'kill', [], -1)

" file header + new-file template (filehdr.py)
" :AH          prepend the // +FHDR header to the current buffer
" :ATpl new.v  generate a new-file skeleton and open it (tb suffix => tb skeleton)
command! -nargs=0 AH   call s:Run('verilog_tooling.filehdr', 'header', [], -1)
command! -nargs=1 -complete=file ATpl call s:NewFromTemplate(<f-args>)

function! s:NewFromTemplate(fname) abort
    let l:save_pp = $PYTHONPATH
    let $PYTHONPATH = s:py_path . (empty(l:save_pp) ? '' : ':' . l:save_pp)
    let l:cmdstr = join(map([s:Python(), '-m', 'verilog_tooling.filehdr', 'template', '-f', a:fname, '--force'], 'shellescape(v:val)'), ' ')
    let l:result = system(l:cmdstr)
    let $PYTHONPATH = l:save_pp
    if v:shell_error != 0
        echohl ErrorMsg | echom '[verilog_tooling] ' . substitute(l:result, '\n$', '', '') | echohl None
        return
    endif
    execute 'edit ' . fnameescape(a:fname)
endfunction

" generators (AM/AME need the cursor line; APM/AFM are buffer-wide)
command! -nargs=0 AM   call s:Run('verilog_tooling.gen', 'am',  ['--line', line('.')], -1)
command! -nargs=0 AME  call s:Run('verilog_tooling.gen', 'ame', ['--line', line('.')], -1)
command! -nargs=0 APM  call s:Run('verilog_tooling.gen', 'apm', [], -1)
command! -nargs=0 AFM  call s:Run('verilog_tooling.gen', 'afm', [], -1)

" verilog-mode wire/reg auto-declaration
command! -nargs=0 AW   call s:Run('verilog_tooling.wire', 'aw', s:InterfaceArgs(), -1)
command! -nargs=0 AREG call s:Run('verilog_tooling.wire', 'ar', s:InterfaceArgs(), -1)

" verilog-mode input/output/inout port auto-declaration (AUTOOUTPUT + AUTOINPUT + AUTOINOUT)
command! -nargs=0 AIO  call s:Run('verilog_tooling.inout', 'aio', s:InterfaceArgs(), -1)

" Auto-generate the new-file skeleton when creating a .v/.sv file
" (the old automatic.vim AutoTemplate BufNewFile behaviour, Python-backed).
autocmd BufNewFile *.v,*.sv call s:AutoTemplateBuffer()

function! s:AutoTemplateBuffer() abort
    if line('$') != 1 || getline(1) !=# ''
        return  " only on a truly empty new buffer
    endif
    let l:fname = expand('%:p')
    if l:fname ==# ''
        return
    endif
    call s:NewFromTemplate(l:fname)
endfunction

" ---------------------------------------------------------------------
" Default leader mappings.  Installed on VimEnter so a mapleader set
" ANYWHERE in the vimrc is honored — plugin managers (pathogen/vim-plug)
" source this file before the lines below plug#end()/pathogen#infect()
" run, and <leader> binds to whatever mapleader is at map-creation time.
" Each lhs is installed only when it has no exact mapping (maparg — NOT
" mapcheck, which also matches a shorter lhs prefix like <leader>a for
" <leader>ad and would skip every two-letter map), so a mapping from your
" .vimrc or another plugin always wins.  Disable the set with:
"   let g:verilog_tooling_no_mappings = 1
function! s:DefMap(lhs, cmd) abort
    if empty(maparg(a:lhs, 'n'))
        execute 'nnoremap ' . a:lhs . ' :' . a:cmd . '<cr>'
    endif
endfunction

function! s:InstallDefaultMaps() abort
    if exists('g:verilog_tooling_no_mappings')
        return
    endif
    call s:DefMap('<leader>a',    'AALL')
    call s:DefMap('<leader>ad',   'AD')
    call s:DefMap('<leader>adt',  'ADT')
    call s:DefMap('<leader>ait',  'AIT')
    call s:DefMap('<leader>aiu',  'AIU')
    call s:DefMap('<leader>aiu1', 'AIU1')
    call s:DefMap('<leader>ar',   'AR')
    call s:DefMap('<leader>aw',   'AW')
    call s:DefMap('<leader>arg',  'AREG')
    call s:DefMap('<leader>aif',  'AIF')
    call s:DefMap('<leader>adf',  'ADF')
    call s:DefMap('<leader>apf',  'APF')
    call s:DefMap('<leader>af',   'AF')
    call s:DefMap('<leader>am',   'AM')
    call s:DefMap('<leader>ame',  'AME')
    call s:DefMap('<leader>eai',  'EAI')
    call s:DefMap('<leader>eap',  'EAP')
    call s:DefMap('<leader>aio',  'AIO')
    call s:DefMap('<leader>d',    'KI')
endfunction

augroup verilog_tooling_maps
    autocmd!
    autocmd VimEnter * call s:InstallDefaultMaps()
augroup END
if exists('v:vim_did_enter') && v:vim_did_enter
    " sourced after startup (lazy load / manual :source): install now
    call s:InstallDefaultMaps()
endif
