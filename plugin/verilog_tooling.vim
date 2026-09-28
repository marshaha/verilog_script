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
"
" Commands (same names as the automatic.vim bindings):
"   AALL                      full AUTO expansion (EAP+EAI+AW+AREG+AD+AR+AF,
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
" A count selects the Nth /*autoinst*/ instance (0-based): :1AIT → first only.
"
" Default leader mappings are installed at the bottom of this file (only
" when the lhs is still unmapped — your own mappings always win):
"   <leader>a   :AALL     <leader>ad  :AD     <leader>adt :ADT
"   <leader>ait :AIT      <leader>aiu :AIU    <leader>aiu1:AIU1
"   <leader>ar  :AR       <leader>aw  :AW     <leader>arg :AREG
"   <leader>aif :AIF      <leader>adf :ADF    <leader>apf :APF  <leader>af :AF
"   <leader>am  :AM       <leader>ame :AME    <leader>d   :KI
"   <leader>eai :EAI      <leader>eap :EAP
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

" Run python -m {mod} {cmd} [extra] on the current buffer.
" a:which >= 0 passes --which (Nth /*autoinst*/ instance).
function! s:Run(mod, cmd, extra, which) abort
    if !s:CheckPython()
        return
    endif
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

    " PYTHONPATH covers src/ so no editable install is needed.
    let l:save_pp = $PYTHONPATH
    let $PYTHONPATH = s:py_path . (empty(l:save_pp) ? '' : ':' . l:save_pp)
    " system() with a list misfires under some locales; build a string cmd.
    let l:cmdstr = join(map(copy(l:argv), 'shellescape(v:val)'), ' ')
    let l:result = system(l:cmdstr)
    let $PYTHONPATH = l:save_pp

    if v:shell_error != 0
        echohl ErrorMsg
        echom '[verilog_tooling] ' . substitute(l:result, '\n$', '', '')
        echohl None
        call delete(l:in)
        return
    endif

    let l:new = readfile(l:out)
    let l:old = getline(1, '$')
    if l:new !=# l:old
        let l:view = winsaveview()
        " minimal-diff update: only replace the lines that actually differ,
        " so Vim redraws less and marks/folds outside the change survive
        let l:min = min([len(l:old), len(l:new)])
        let l:i = 0
        while l:i < l:min && l:old[l:i] ==# l:new[l:i]
            let l:i += 1
        endwhile
        let l:j = 0
        while l:j < l:min - l:i && l:old[len(l:old) - 1 - l:j] ==# l:new[len(l:new) - 1 - l:j]
            let l:j += 1
        endwhile
        let l:mid = l:new[l:i : len(l:new) - l:j - 1]
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
    else
        echom '[verilog_tooling] ' . a:cmd . ': no changes'
    endif
    call delete(l:in)
    call delete(l:out)
endfunction

function! s:InstCmd(cmd, extra) abort
    " v:count1 is 1 when no count given; map to -1 = all instances.
    call s:Run('verilog_tooling.inst', a:cmd, a:extra, v:count1 - 1)
endfunction

" Run the full AUTO expansion set in emacs verilog-batch-auto order:
" EAP (AUTOINSTPARAM) -> EAI (AUTOINST) -> AW (AUTOWIRE) -> AREG (AUTOREG)
" -> AD (autodef) -> AR (autoarg) -> AF (all format).
function! s:AutoAll() abort
    call s:Run('verilog_tooling.inst', 'eap', [], -1)
    call s:Run('verilog_tooling.inst', 'eai', get(g:, 'verilog_tooling_eai_flags', []), -1)
    call s:Run('verilog_tooling.wire', 'aw', [], -1)
    call s:Run('verilog_tooling.wire', 'ar', [], -1)
    call s:Run('verilog_tooling.autodef', 'adt', [], -1)
    call s:Run('verilog_tooling.arg', 'ar', [], -1)
    call s:Run('verilog_tooling.inst', 'af', [], -1)
endfunction

command! -nargs=0 AALL call s:AutoAll()

command! -count=0 AIT  call s:InstCmd('ait', [])
command! -count=0 AIU  call s:InstCmd('aiu', [])
command! -count=0 AIU1 call s:InstCmd('aiu1', [])
command! -count=0 KI   call s:InstCmd('kill', [])
command! -count=0 EAI  call s:InstCmd('eai', get(g:, 'verilog_tooling_eai_flags', []))
command! -count=0 EAP  call s:InstCmd('eap', [])

command! -nargs=0 AIF  call s:Run('verilog_tooling.inst', 'aif', [], -1)
command! -nargs=0 APF  call s:Run('verilog_tooling.inst', 'apf', [], -1)
command! -nargs=0 ADF  call s:Run('verilog_tooling.inst', 'adf', [], -1)
command! -nargs=0 AF   call s:Run('verilog_tooling.inst', 'af', [], -1)

command! -nargs=0 AR   call s:Run('verilog_tooling.arg', 'ar', [], -1)
command! -nargs=0 KAR  call s:Run('verilog_tooling.arg', 'kill', [], -1)
command! -nargs=0 AD   call s:Run('verilog_tooling.autodef', 'adt', [], -1)
command! -nargs=0 ADT  call s:Run('verilog_tooling.autodef', 'adt', [], -1)
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
command! -nargs=0 AW   call s:Run('verilog_tooling.wire', 'aw', [], -1)
command! -nargs=0 AREG call s:Run('verilog_tooling.wire', 'ar', [], -1)

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
