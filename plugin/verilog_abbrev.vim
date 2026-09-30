" verilog_abbrev.vim — buffer-local insert-mode abbreviations for
" Verilog/SystemVerilog files (ported from automatic.vim's global iabbrevs,
" scoped to SV buffers so they never leak into other file types).
"
"   <=     ->  <= #`RD      (non-blocking assign with `RD delay)
"   beg    ->  begin
"   begni  ->  begin        (typo fix)
"
" Triggered for *.v, *.vh, *.sv, *.svh buffers.
" OFF by default — enable in your vimrc with:
"   let g:verilog_abbrev_enable = 1

if exists('g:loaded_verilog_abbrev')
    finish
endif
let g:loaded_verilog_abbrev = 1

augroup verilog_abbrev
    autocmd!
    autocmd BufNewFile,BufRead *.v,*.vh,*.sv,*.svh call s:VerilogAbbrev()
augroup END

function! s:VerilogAbbrev() abort
    if !get(g:, 'verilog_abbrev_enable', 0)
        return
    endif
    iabbrev <buffer> <= <= #`RD
    iabbrev <buffer> beg begin
    iabbrev <buffer> begni begin
endfunction
