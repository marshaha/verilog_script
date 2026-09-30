# verilog_script

[English](README.md) | **简体中文**

Emacs `verilog-mode` 的 AUTO 展开功能和 `automatic.vim` 命令集的 Python
重写版——一个自包含的 Vim 插件，后端是一个小型 Python 包。

无 pip 依赖（只用 Python 标准库），不需要安装 emacs。
执行命令前**不需要先保存文件**——命令直接作用于当前正在编辑的
buffer 并就地更新（撤销历史、标记、折叠都保留）。

## 环境要求

- Vim 8.2+
- `PATH` 中有 **Python ≥ 3.8**（或用 `g:verilog_tooling_python` 指定解释器）

## 安装

### 插件管理器（推荐）

```vim
" vim-plug
Plug 'marshaha/verilog_script'

" Vundle
Plugin 'marshaha/verilog_script'

" packer.nvim
use 'marshaha/verilog_script'
```

### 一键手动安装

```bash
git clone https://github.com/marshaha/verilog_script.git
cd verilog_script
./install.sh            # 把 plugin/ 和 python/ 复制到 ~/.vim
./install.sh --check    # 只做环境检查
```

## 快速上手

在想生成代码的地方放好 AUTO 标记，然后按 `<leader>a`
（默认 leader 下是 `\a`；`let mapleader = "-"` 时是 `-a`）
或执行 `:AALL`：

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

变成：

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
reg                                     done;   // 未驱动的 output -> reg
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

（默认连接就是端口名本身、带上位宽——不会编造任何后缀）

重复执行是幂等的；`KI`/`KAR`/`KADT` 可以把展开的区域折叠回标记。

## 命令一览

| 命令 | 作用 | 按键 |
|---|---|---|
| `AALL` | 一次跑完整套 AUTO：EAP → EAI → AIO → AW → AREG → AD → AR → AF | `<leader>a` |
| `EAI` / `EAP` | verilog-mode 的 AUTOINST / AUTOINSTPARAM | `<leader>eai` `eap` |
| `AIT` | 按模块定义（重）建实例连接 | `<leader>ait` |
| `AIU` / `AIU1` | 最小差异的实例更新（保留手动连接） | `<leader>aiu` `aiu1` |
| `KI` | 把实例折叠回 `/*autoinst*/` 空壳 | `<leader>d` |
| `AD` / `ADT` | 重新生成 `/*autodef*/` 的 wire/reg/integer/genvar 声明 | `<leader>ad` `adt` |
| `KADT` | 折叠 `/*autodef*/` 区域 | |
| `AR` | 重新生成 `/*autoarg*/` 模块头端口表 | `<leader>ar` |
| `KAR` | 折叠 `/*autoarg*/` 区域 | |
| `AW` / `AREG` | AUTOWIRE / AUTOREG | `<leader>aw` `arg` |
| `AIO` | AUTOOUTPUT + AUTOINPUT（封装模块端口生成） | `<leader>aio` |
| `AF` | 对齐：端口、wire/reg、parameter/localparam、实例 | `<leader>af` |
| `AIF` `APF` `ADF` | 单独的各路对齐 | `<leader>aif` `apf` `adf` |
| `AM` `AME` | 把光标下的单词变成实例空壳 | `<leader>am` `ame` |
| `APM` `AFM` | `/*autopara*/` / `/*autofsm*/` 展开 | |
| `AH` / `ATpl {file}` | 文件头 / 按模板建新文件 | |
| `BPN` `BP` `BA` | always 块片段 | `<leader>bpn` `bp` `ba` |

在 EAI/AIT/AIU/AIU1 前加 `{count}` 可只处理第 N 个 `/*autoinst*/`
实例（`:1AIT` → 只处理第一个）。

每个命令都会报告做了什么，例如
`[verilog_tooling] eai: line 515: 1005 -> 937 line(s)`（或 `no changes`）。

### 性能（大型 SoC 顶层）

`AALL` 在**单个 Python 进程**里跑完整个流水线，八步共享同一份模块表；
模块文件通过目录列表缓存定位、用线程池读取（对 NFS 友好——没有
"每个模块×每个目录一次 stat"的风暴）。在 199 个文件的工程上实测：
比分开跑八个命令快 3.4 倍，输出逐字节一致。

## 命令详解

### AALL —— 按正确顺序跑全部

在单个 Python 进程中按 emacs `verilog-batch-auto` 的顺序执行
EAP → EAI → AIO → AW → AREG → AD → AR → AF。输出与依次执行八个命令
逐字节一致，但模块文件只解析和读取一次。
支持 `g:verilog_tooling_eai_flags`（如 `--sort`）。

### EAI —— verilog-mode 的 AUTOINST

展开 `/*AUTOINST*/`：丢弃旧的展开结果，按子模块定义重新连接所有
引脚，按声明顺序分 `// Interfaces` / `// Outputs` / `// Inouts` /
`// Inputs` 四节（`--sort` 可在组内排序）。默认连接是端口名带位宽
——`.dout (dout[7:0])`。注意：

- EAI 是**全量重置**（emacs 语义）：要保留自定义连接，请写进
  `AUTO_TEMPLATE`——由模板驱动的引脚会标 `// Templated`。
  想原地保留手写连接，请改用 AIU/AIU1。
- 标记行**之前**已经手动连好的引脚会保留。
- `/*AUTOINST("regex")*/` 只保留匹配的引脚；`?!` 前缀表示排除
  （忽略大小写；emacs 和 Python 两种正则方言都支持）。
- 开启 star 展开后，SystemVerilog `.*` 实例也会展开；
  interface 端口连为 `.bus (bus.master)`。
- 找不到模块文件的实例会跳过并给出警告，列出搜索过的目录。

### EAP —— AUTOINSTPARAM

填充实例的 `#(...)` 参数表（`/*AUTOINSTPARAM*/` 标记，或 AIT/EAI
新建实例时）。优先级规则见下文
[AUTOINSTPARAM (EAP)](#autoinstparam-eap)。

### AIT —— （重）建实例

automatic.vim 的 AutoInst：先删除当前连接，再按模块定义重新生成
（默认同名连接；有 AUTO_TEMPLATE 声明的按模板）。在空壳
`fifo u0_fifo (/*autoinst*/);` 上执行可生成完整引脚表，也可用于
强制干净重建。实例行上写 `--oneline` 注释可把所有连接压到一行。

### AIU / AIU1 —— 最小差异更新

适合日常"子模块改了端口"的维护：

- `AIU1` 保留所有现有行，在 `);` 前追加新端口（标 `// INST_NEW`），
  把已删除的端口注释掉（标 `// INST_DEL`）。
- `AIU` 做同样的更新，但按模块端口顺序重写实例，保留每条存活连接
  行的原文——手写的 `.port (custom_sig)` 连接原样保留。
  `AUTO_TEMPLATE` 声明过的端口仍按模板连接。

### KI —— 折叠实例

删除生成的引脚表，只留 `mod inst (/*autoinst*/);` 空壳。
下次 EAI/AIT 从零重建。

### AD / ADT —— 声明所有未声明的信号

重新生成 `/*autodef*/` 区域：io wire、触发器和组合逻辑寄存器、
assign 线网、实例输出驱动的线网，以及 for 循环变量
（`integer`/`genvar`），位宽从驱动侧推导。未驱动的 output 声明为
`reg`。完整规则见下文 [/*autodef*/ (AD/ADT)](#autodef-adadt)。
`KADT` 把区域折叠回标记。

### AR —— 模块头端口表

根据端口声明重新生成模块头里的 `/*autoarg*/`：
`//Inputs` / `//Outputs` / `//Inouts` 三节，名字缩进 4 空格、
超过第 40 列换行。兼容头部声明（`input clk,`）、一行多个声明
（`input a, input b`）、跨行声明、unpacked 维度（`val[3:0]`）。
放错位置的标记（不在模块头内）会原样保留不动。
`KAR` 把端口表折叠回标记。

### AW / AREG —— AUTOWIRE / AUTOREG

`/*AUTOWIRE*/` 为实例输出驱动的线网声明 wire；`/*AUTOREG*/` 为没有
驱动的模块 output 声明 `reg`。见下文
[AUTOWIRE / AUTOREG](#autowire--autoreg-awareg)。

### AF 家族 —— 对齐

只在当前 buffer 内做对齐（不需要模块文件）：

- `APF` 对齐端口声明（`input`/`output`/`inout`），
- `ADF` 对齐 `wire`/`reg`/`logic` 声明以及 `parameter`/`localparam`
  （`=` 号对齐），
- `AIF` 对齐实例端口连接，
- `AF` 一次跑全部三种。

幂等；已经对齐时报告 `no changes`。

### AM / AME —— 实例空壳

把光标下的单词变成该行的实例空壳：
`fifo` → `fifo u0_fifo (/*autoinst*/);`（AM，automatic.vim 风格），
AME 生成 emacs 风格的空壳。实例编号按 buffer 中该模块已有实例数
递增。

### APM / AFM —— 参数 / 状态机骨架

`/*autopara*/ (A, B=2, C)` 展开为对齐的 `parameter` 声明。
`/*autofsm*/ (IDLE,RUN,DONE) state nstate` 展开为状态 localparam
加两段式 FSM 骨架（状态寄存器 + 次态组合逻辑），状态位宽按状态数
自动推导。

### AH / ATpl —— 文件头与新文件

`AH` 在文件开头插入文件头注释块（`// +FHDR`）。
`ATpl foo.v` 按工程骨架创建新文件并打开——文件名带 `_tb`/`tb`
后缀时生成 testbench 骨架。新建空的 `.v`/`.sv` buffer 时会自动
套骨架（BufNewFile）。

### BPN / BP / BA —— always 块片段

在光标处插入 always 骨架：`BPN` —
`always @(posedge clk or negedge rst_n)` 带 `if (!rst_n)` 复位分支；
`BP` — `always @(posedge clk)`；`BA` — 组合逻辑 `always @(*)`。

## 默认按键映射

插件在 `VimEnter` 时安装这些 normal 模式 leader 映射——所以 vimrc
里任何位置设置的 `mapleader` 都能生效——并且只在该按键还没有
映射时才安装，你自己的映射永远优先：

| 按键 | 命令 | 按键 | 命令 |
|---|---|---|---|
| `<leader>a` | `AALL` | `<leader>af` | `AF` |
| `<leader>ad` | `AD` | `<leader>aif` `adf` `apf` | `AIF` `ADF` `APF` |
| `<leader>adt` | `ADT` | `<leader>am` `ame` | `AM` `AME` |
| `<leader>ait` | `AIT` | `<leader>aw` | `AW` |
| `<leader>aiu` / `aiu1` | `AIU` / `AIU1` | `<leader>arg` | `AREG` |
| `<leader>aio` | `AIO` | | |
| `<leader>ar` | `AR` | `<leader>eai` `eap` | `EAI` `EAP` |
| `<leader>d` | `KI` | `<leader>bpn` `bp` `ba` | `BPN` `BP` `BA` |

全部禁用：

```vim
let g:verilog_tooling_no_mappings = 1
```

## 插入缩写（可选，默认关闭）

`plugin/verilog_abbrev.vim` 移植了 automatic.vim 的全局插入缩写，
并且只在 `*.v`/`*.vh`/`*.sv`/`*.svh` buffer 内生效：

| 输入 | 展开为 |
|---|---|
| `<=` | `<= #\`RD` |
| `beg` | `begin` |
| `begni`（笔误） | `begin` |

**默认关闭**，在 vimrc 中开启：

```vim
let g:verilog_abbrev_enable = 1
```

## 模块文件如何查找

EAI/AIT/AW 需要读取子模块的源文件才能拿到端口。搜索顺序：

1. buffer 底部 Local Variables 块里的 `verilog-library-directories` /
   `verilog-library-flags`（相对路径相对**真实文件所在目录**解析，
   `~` 和开头的 `$VAR` 会展开）
2. 其中 `-f file.vc` 列出的库文件（递归）
3. 正在编辑的文件所在目录，以及同一 buffer 内定义的模块
4. `g:verilog_tooling_libdirs` / CLI 的 `-y` 目录

文件底部典型的 Local Variables 块：

```verilog
// Local Variables:
// verilog-library-directories:("../pmu" "../por_rst" "../../common/clk/rtl/com")
// verilog-library-flags:("-f ../mm_file_dir.vc")
// verilog-auto-inst-param-value:t
// End:
```

它指向的 `.vc` 文件（每行一条；支持 `#`/`//` 注释）：

```
# mm_file_dir.vc — 模块搜索清单
-y  $PROJ_DIR/design/rtl/axi_1_to_n
-y  $PROJ_DIR/design/rtl/mm_cmd_dma
-y  $PROJ_DIR/design/rtl
+incdir+$PROJ_DIR/design/include
-v  $PROJ_DIR/design/rtl/legacy/pll_wrap.v
+libext+.v+.sv
```

支持的 vc 条目：`-y dir`、`+incdir+dir`、`-I dir`、`-v file`、
`+libext+.v+.sv…`。找不到模块时，该实例会被跳过并给出
`[verilog_tooling]` 警告，列出搜索过的目录。

## AUTO_TEMPLATE (EAI/EAP)

模板按实例定制连接。写在实例上方的注释里（或
`verilog-inst-file:` 指定的文件中）：

```verilog
/* mm_cdma_parse AUTO_TEMPLATE (
    .CDMA_REQ_NUM        (CDMA_INTERNAL_NUM),
    .cmd_apb_fifo_rdata  (cmd_apb_fifo_rdata_r[]),
    .cdma_\(ar\|aw\)id_s (cdma_\1id_internal[1:0]),
    .int_req             (int_req_@),
) */
mm_cdma_parse u_parse (/*autoinst*/);
```

- **连接**里的 `@` 展开为实例编号（实例名中的第一组数字；可用
  `AUTO_TEMPLATE "my_re_\([0-9]+\)"` 自定义 `@` 的匹配）
- **端口模式**里的 `@` 匹配一组数字：`.port_@ (sig_@)` 覆盖
  `port_0 … port_3`
- `[]` 展开为被连端口的声明位宽（`rdata[7:0]`；标量端口则去掉）
- 正则端口模式里的 `\(...\)` 分组在连接里用 `\1`、`\2`… 回代
- **Emacs 和 Python 两种正则方言都支持——自动识别**（此处以及
  `AUTO_TEMPLATE "regexp"`、`/*AUTOINST("regex")*/` 过滤器和
  `verilog-typedef-regexp`)：Emacs 的 `\(...\)`/`\|` 和 Python 的
  `(...)`/`|` 甚至可以混写；Python 风格的 `\g<1>` 回代与 `\1` 并存
- `@"expr"` 对表达式求值——Python(`@"'pre_%d' % @"`）或带括号的
  elisp(`@"(downcase vl-name)"`；支持的形式：`substring`、
  `downcase`、`concat`、`if`、`equal`、算术、`let`、`setq`；
  绑定变量 `vl-name`、`vl-cell-name`、`vl-width`、`vl-dir`)
- 标记前的 `/*AUTO_LISP(expr)*/` 会先求值一段 Python 绑定，供
  `@"..."` 表达式引用

没有模板条目的端口连接同名线网（带位宽）；如果线网还没声明，
AW/AD 会帮你声明。支持 SystemVerilog `interface` 端口（含
modport):`cpu_bus.master bus` 连为 `.bus (bus.master)`。
开启 star 展开后 `.*` 实例按 AUTOINST 处理。

## AUTOINSTPARAM (EAP)

`/*AUTOINSTPARAM*/`（以及 AIT/EAI 新建实例的 `#(...)`）按如下
优先级确定参数连接：

1. 该参数的 `AUTO_TEMPLATE` 条目
2. 父模块同名的 parameter/localparam（**符号化透传**——不会替换成
   数字，这样上层再实例化本模块时依然成立）
3. 子模块的默认值（在父模块可见时）
4. 否则省略该参数（子模块默认值生效）

在 Local Variables 中设置 `verilog-auto-inst-param-value:t`（或 CLI
加 `--param-value`）后，实例 `#(...)` 的覆盖值还会代入口位宽和
模板连接（常量表达式和 `$clog2` 会求值折叠）。

## /*autodef*/ (AD/ADT)

把所有未声明的信号重新生成到固定分节中，位宽从驱动侧推导
（字面量、位选/片选、比较运算、信号链接、子模块端口位宽、
`+:`/`-:` 索引片选）。引用 parameter/`` `define`` 的位宽保持符号化。

```
// Define io wire here            没有显式 wire/reg 的端口
// Define flip-flop registers here    时钟 always 里 <= 的左值
// Define combination registers here   其他 always 里 = 的左值
// Define wires here                  assign 的左值
// Define inst wires here             实例输出驱动的线网
// Define integer here                for 循环变量
// Unresolved define signals here     无法推导的（以 // 注释形式）
```

类型规则：

- `always` 块左值 → `reg`;`assign` / 实例驱动 → `wire`
- **未驱动的 `output` → `reg`**（接下来你会用 always 块驱动它；
  与 AREG 一致）。input/inout 和已有驱动的 output 保持 `wire`
- **for 循环变量自动声明**:`generate` 循环用 `genvar`，过程块
  (always/function/task）循环用 `integer`，支持两级嵌套；
  `mem[i][j]`、`fifo[i*W +: W]` 这类循环索引左值会贡献 unpacked
  维度和位宽推导
- 手写声明保留（数值上可证明过时的位宽会原地扩大）；在声明后加
  `//DT`(don't touch）可豁免
- 全面支持 SystemVerilog 的 `logic`

## AUTOWIRE / AUTOREG (AW/AREG)

`/*AUTOWIRE*/` 为 `/*autoinst*/` 实例输出驱动的线网声明 wire
（带 `// From u_x of mod.v` 注释）。`/*AUTOREG*/` 为没有驱动
（assign/always/实例）的模块 output 声明 `reg`。两者都跳过已声明的
信号，位宽保持符号化。

## AUTOINPUT / AUTOOUTPUT (AIO)

verilog-mode 的封装模块端口生成（`:AIO` 先 AUTOOUTPUT 后 AUTOINPUT，
`AALL` 也包含这两步）：

- `/*AUTOINPUT*/` 为每个"喂给实例 input、但模块内未声明也无驱动"的
  线网声明 `input` 端口（注释 `// To u_x of mod.v`）；
- `/*AUTOOUTPUT*/` 为每个"由实例 output 驱动、不是本模块端口、也不
  喂给其他实例"的线网声明 `output` 端口（喂给其他实例的留在内部，
  归 AUTOWIRE 管）。

标记放在模块头括号内时按 Verilog-2001 风格展开（逗号分隔，带
verilog-mode 的开/闭逗号修补）；放在模块体内则是 1995 风格（`;`）。
每个标记支持正则过滤——`/*AUTOINPUT("^i_")*/`，`?!` 前缀取反——
也支持 `verilog-auto-input-ignore-regexp` /
`verilog-auto-output-ignore-regexp` 文件局部变量。位宽取自子模块端口
并代入实例 `#(...)` 参数（verilog-mode 自己只对 AUTOINST 重写过的
连接才能拿到位宽；标记紧跟在模块头 `(` 后时 emacs 会直接报错，我们
正常展开）。由 `assign` 驱动的线网不会被声明成 input，拼接/表达式
连接直接跳过——这两种情况生成了也编译不过。

## 目录结构

```
plugin/verilog_tooling.vim   Vim 前端（自动加载）
plugin/automatic.vim         文件头/波形片段（BPN/BP/BA, AddClk/AddSig/AddBus）
plugin/verilog_abbrev.vim    .v/.vh/.sv/.svh buffer 局部缩写（默认关闭，需显式开启）
python/verilog_tooling/      Python 包（只用标准库）
doc/verilog_tooling.txt      vim 帮助文档
install.sh                   一键安装 / 环境检查
```

可选设置：

```vim
let g:verilog_tooling_python = '/usr/bin/python3'   " 指定 Python 解释器
let g:verilog_tooling_libdirs = ['/path/to/rtl']    " 额外的 -y 搜索目录
let g:verilog_tooling_eai_flags = ['--sort']        " EAI 的额外参数
let g:verilog_tooling_no_mappings = 1               " 不装默认按键映射
let g:verilog_abbrev_enable = 1                     " 开启插入缩写（默认关闭）
```
