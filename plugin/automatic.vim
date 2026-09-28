" automatic.vim — waveform snippets & always-block inserters (KEEP set)
"
" This is the slimmed-down remainder of the original automatic.vim.
" All code-generation commands (AIT/AIU/AI/AD/ADT/AR/APF/ADF/AIF/AF/AM/AME/
" AEM/AFM/APM, AddHeader, AutoTemplate) have been moved to the Python
" `verilog_tooling` package — see verilog_tooling.vim, which calls it.
" What remains here is only the Vim-interactive editing helpers that have no
" Python equivalent (they insert/draw text and move the cursor in a live
" buffer), plus the BPN/BP/BA always-block snippet inserters.

" ---------------------------------------------------------------
" state for the waveform draw functions
let s:sig_offset = 13
let s:clk_period = 8
let s:clk_num = 16
let s:wave_max_wd = s:sig_offset + s:clk_num*s:clk_period

" ---------------------------------------------------------------
" always-block snippet inserters (command bindings preserved)
command! BPN : call AlBpn()
command! BP  : call AlBp()
command! BA  : call AlB()

" Default leader mappings (skipped when the lhs is already mapped; disable
" with:  let g:verilog_tooling_no_mappings = 1)
if !exists('g:verilog_tooling_no_mappings')
    if empty(mapcheck('<leader>bpn', 'n'))
        nnoremap <leader>bpn :BPN<cr>
    endif
    if empty(mapcheck('<leader>bp', 'n'))
        nnoremap <leader>bp :BP<cr>
    endif
    if empty(mapcheck('<leader>ba', 'n'))
        nnoremap <leader>ba :BA<cr>
    endif
endif

function! AddClk() "{{{2
	let ret = []
	let ret0 = "//  .   .   ."
	let ret1 = "//          +"
	let ret2 = "// clk      |"
	let ret3 = "//          +"
	let format = '%' . s:clk_period/2 . 'd'
	for idx in range(1,s:clk_num)
		let ret0 = ret0 . printf(format,idx) . repeat(' ',s:clk_period/2)
		for idx2 in range(1,s:clk_period/2-1)
			let ret1 = ret1 . '-'
			let ret2 = ret2 . ' '
			let ret3 = ret3 . ' '
		endfor
		let ret1 = ret1 . '+'
		let ret2 = ret2 . '|'
		let ret3 = ret3 . '+'
		for idx2 in range(1,s:clk_period/2-1)
			let ret1 = ret1 . ' '
			let ret2 = ret2 . ' '
			let ret3 = ret3 . '-'
		endfor
		let ret1 = ret1 . '+'
		let ret2 = ret2 . '|'
		let ret3 = ret3 . '+'
	endfor
	call add(ret,ret0)
	call add(ret,ret1)
	call add(ret,ret2)
	call add(ret,ret3)
    let lnum = line(".")
    let col = col(".")
	call append(line("."),ret)
    call cursor(lnum+4,col)
endfunction "}}}2

function! AddSig() "{{{2
	let ret = []
	let ret0 = "//          "
	let ret1 = "// sig      "
	let ret2 = "//          "
	for idx in range(s:sig_offset,s:wave_max_wd)
		let ret0 = ret0 . " "
		let ret1 = ret1 . " "
		let ret2 = ret2 . "-"
	endfor
	call add(ret,ret0)
	call add(ret,ret1)
	call add(ret,ret2)
    let lnum = line(".")
    let col = col(".")
	call append(line("."),ret)
    call cursor(lnum+3,col)
endfunction "}}}2

function! AddBus() "{{{2
	let ret = []
	let ret0 = "//          "
	let ret1 = "// bus      "
	let ret2 = "//          "
	for idx in range(s:sig_offset,s:wave_max_wd)
		let ret0 = ret0 . "-"
		let ret1 = ret1 . " "
		let ret2 = ret2 . "-"
	endfor
	call add(ret,ret0)
	call add(ret,ret1)
	call add(ret,ret2)
    let lnum = line(".")
    let col = col(".")
	call append(line("."),ret)
    call cursor(lnum+3,col)
endfunction "}}}2

function! AddNeg() "{{{2
    let lnum = s:GetSigNameLineNum()
    if lnum == -1
        return
    endif
    let line = getline(lnum)
    if line =~ 'neg\s*$'
        return
    endif
    call setline(lnum,line." neg")
endfunction "}}}2

function! AddBlk() "{{{2
	let ret = []
	let ret0 = "//          "
	for idx in range(s:sig_offset,s:wave_max_wd)
		let ret0 = ret0 . " "
	endfor
	call add(ret,ret0)
    let lnum = line(".")
    let col = col(".")
	call append(line("."),ret)
    call cursor(lnum+1,col)
endfunction "}}}2

function! s:My_mod(int1,int2) "{{{2
	let ret = a:int1
	while 1
		if ret >= a:int2
			let ret = ret - a:int2
		else
			break
		endif
	endwhile
	return ret
endfunction "}}}2

function! s:GetSigNameLineNum() "{{{2
	let lnum = -1
	let cur_lnum = line(".")
	if getline(cur_lnum) =~ '^\/\/\s*\w\+'
		let lnum = cur_lnum
	elseif getline(cur_lnum-1) =~ '^\/\/\s*\w\+'
		let lnum = cur_lnum-1
	elseif getline(cur_lnum+1) =~ '^\/\/\s*\w\+'
		let lnum = cur_lnum+1
	endif
	return lnum
endfunction "}}}2
function! AlBpn() "{{{2
    let lnum = line(".")
    for idx in range(1,7)
        call append(lnum,"")
    endfor
    call setline(lnum+1,"always @(posedge clk or negedge rst_n) begin")
    call setline(lnum+2,"    if(!rst_n) begin")
    call setline(lnum+3,"        ")
    call setline(lnum+4,"    end else if() begin")
    call setline(lnum+5,"    end else begin")
    call setline(lnum+6,"    end")
    call setline(lnum+7,"end")
    call cursor(lnum+3,9)
endfunction "}}}2

function! AlB() "{{{2
    let lnum = line(".")
    for idx in range(1,3)
        call append(lnum,"")
    endfor
    call setline(lnum+1,"always @(*) begin")
    call setline(lnum+2,"    ")
    call setline(lnum+3,"end")
    call cursor(lnum+2,5)
endfunction "}}}2

function! AlBnn() "{{{2
    let lnum = line(".")
    for idx in range(1,7)
        call append(lnum,"")
    endfor
    call setline(lnum+1,"always @(negedge clk or negedge rst_n) begin")
    call setline(lnum+2,"    if(!rst_n) begin")
    call setline(lnum+3,"        ")
    call setline(lnum+4,"    end else if() begin")
    call setline(lnum+5,"    end else begin")
    call setline(lnum+6,"    end")
    call setline(lnum+7,"end")
    call cursor(lnum+3,9)
endfunction "}}}2

function! AlBp() "{{{2
    let lnum = line(".")
    for idx in range(1,6)
        call append(lnum,"")
    endfor
    call setline(lnum+1,"always @(posedge clk) begin")
    call setline(lnum+2,"    if() begin")
    call setline(lnum+3,"        ")
    call setline(lnum+4,"    end else begin")
    call setline(lnum+5,"    end")
    call setline(lnum+6,"end")
    call cursor(lnum+3,9)
endfunction "}}}2

function! AlBn() "{{{2
    let lnum = line(".")
    for idx in range(1,6)
        call append(lnum,"")
    endfor
    call setline(lnum+1,"always @(negedge clk) begin")
    call setline(lnum+2,"    if() begin")
    call setline(lnum+3,"        ")
    call setline(lnum+4,"    end else begin")
    call setline(lnum+5,"    end")
    call setline(lnum+6,"end")
    call cursor(lnum+3,9)
endfunction "}}}2
