# verilog_script

[English](README.md) | **简体中文**

把 Emacs `verilog-mode` 的 AUTO 展开与 `automatic.vim` 的命令集重写为一个
自包含的 Vim 插件，后端是一个小型 Python 包。

无 pip 依赖（只用 Python 标准库），不需要 emacs。命令在活动缓冲区上
**异步**运行（无需先保存）；进度日志进 `:messages`，运行期间若缓冲区被
改动，本次结果作废、不会覆盖你的编辑。`VERILOG_TOOLING_QUIET=1` 可关闭
进度日志。

## 环境要求

- Vim 8.2+
- **Python ≥ 3.7** 在 `PATH` 中（或用 `g:verilog_tooling_python` 指定）

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

## 配置

可选的 Vim 设置——只设你需要的那几个：

```vim
let g:verilog_tooling_python = '/usr/bin/python3'   " 解释器覆盖
let g:verilog_tooling_libdirs = ['/path/to/rtl']    " 额外的 -y 搜索目录
let g:verilog_tooling_eai_flags = ['--sort']        " 额外的 EAI 标志
let g:verilog_tooling_no_mappings = 1               " 不要默认映射
let g:verilog_abbrev_enable = 1                     " 可选插入缩写
let g:verilog_tooling_interfaces = ['axera_apb_interface', 'axera_axi_interface']  " 用户已知的 SV 接口类型
let g:verilog_tooling_quiet = 1                     " 不要管线进度日志
```

`g:verilog_tooling_interfaces` 列出库扫描找不到的 SystemVerilog 接口
类型名（文件不在任何 `-y` 目录下）。这样 `axera_apb_interface.master apb`
这样的端口会被 EAI/AIT/AIU 解析为接口端口——EAI 把它放进
`// Interfaces` 段、连接为 `name.modport`——也不会被 AW/AD 误当作 wire。

进度日志默认开启；关闭办法：vimrc 里 `let g:verilog_tooling_quiet = 1`、
`let $VERILOG_TOOLING_QUIET = 1`，或 shell 里 `VERILOG_TOOLING_QUIET=1`。
关掉之后，只剩插件自己的 `running ...` / 完成摘要消息。

### leader 键

所有默认映射都挂在 Vim 的 `mapleader` 上。默认 leader 是 `\`，所以
AALL 是 `\a`、AD 是 `\ad`，依此类推。在 `.vimrc` 里把 leader 设成
你喜欢的键：

```vim
let mapleader = "-"     " AALL 变成 -a，AD 变成 -ad，……
```

插件在 `VimEnter` 时安装映射，所以 vimrc 里任何位置设置的
`mapleader` 都被尊重；你自己定义的映射永远优先，
`let g:verilog_tooling_no_mappings = 1` 关闭整套默认映射。

### 与 Emacs 不一样的文件局部设置

按文件生效的设置（`verilog-auto-*` / `verilog-library-*` 变量）写在
缓冲区本身里。绝大多数与 verilog-mode 完全一致——同名、同默认值；
完整表格在[核心概念 §3](#3-文件局部变量--verilog-xxxx-value)。不一样的
是这五个：

| 变量 | Emacs | 本工具 |
|---|---|---|
| `verilog-auto-ignore-concat` | 默认 `nil` | **默认开** —— `{...}`/`(...)` 连接不上端口（见 [§1](#1--豁免--让信号不上端口verilog-auto-ignore-concat)） |
| `verilog-auto-read-includes` | 默认 `nil` | **默认开** —— 各分析读穿 `` `include`` |
| `verilog-autodef-user-boundary` | ——（无此变量） | **开** —— 手写声明归用户所有；冲突标注 `//AD_CONFLICT:`（见 [§2](#2-dt--不要动我手写的声明)） |
| `verilog-auto-inst-templated-comment` | ——（标记总是写） | **开**；设 `nil` 去掉 `// Templated` 标记 |
| `verilog-auto-python-file` | ——（无 AUTO_PYTHON） | 指定一个 `.py` 文件，为模板 `@"..."` 表达式提供辅助函数 —— 用法见 [AUTO_PYTHON 与 AUTO_LISP](#auto_python-与-auto_lisp) |

## 核心概念

三条约定决定了本工具生成内容的大部分行为。读命令详解之前先读这一节。

### 1. `{}` 豁免 —— 让信号不上端口（`verilog-auto-ignore-concat`）

AUTOINPUT / AUTOOUTPUT / AUTOINOUT 会把每个实例引脚连接提升为模块端口。
把连接用 `{...}`（或 `(...)`）包住，等于声明*这个信号由我手工管理——
不要放进我的端口表*：

```verilog
sub u_sub (
    .dqs ({user_dqs}),   // 保持为本地信号 —— 不生成端口
    .dq  (user_dq),      // 成为模块的输入/输出端口
    /*autoinst*/);
```

这是让手工连接（比如写在 `/*autoinst*/` 标记**之前**的连接）不上生成
端口的标准办法。细节：

- 豁免只作用于*候选资格*：AUTOWIRE 仍能看到该信号在内部被连接（不会
  给它重复生成一条 wire），所以声明由你自己写——这正是本意；
- 本工具默认 **`verilog-auto-ignore-concat` 开启**（emacs verilog-mode
  默认为 nil——我们有意选了更友好的默认值）；
- 想按名字模式豁免，用 `verilog-auto-input-ignore-regexp`
  / `verilog-auto-output-ignore-regexp` / `verilog-auto-inout-ignore-regexp`。

**手写的体内声明有同样效果**：任何你自己声明的 `wire`/`reg`/`logic`
（在 auto 区域之外）都视为用户意图的内连信号，AUTOINPUT / AUTOOUTPUT /
AUTOINOUT 不会把它提升为端口。如果还希望 AD 不去动这条声明的位宽，
加上 `//DT`。（对 AUTOOUTPUT 这是与 emacs 有意不同的行为——emacs 会把
已声明的 wire 继续提升为端口。）如果一个信号*既*要内连*又*要是端口，
就手写端口声明——AIO 从不碰已经在模块头里的名字。

### 2. `//DT` —— 不要动我手写的声明

行尾的 `//DT` 注释把一行手写内容标记为用户所有。它在两处起作用：

**AD / ADT**（声明再生成）——`/*autodef*/` 区域外的手写 `wire`/`reg`
在推断位宽被证明过期时通常会被*刷新*（位宽增长）；带 `//DT` 则完全
不动——不重写、不打标记：

```verilog
wire [7:0] old_bus; //DT     <- 永不重写、永不标记
```

**AR**（模块头端口表再生成）——模块头里带 `//DT` 的端口在
`/*autoarg*/` 重建后保留：再生成的端口表始终保留这个名字，哪怕它本应
被丢弃：

```verilog
module top (
    input  [7:0] user_port, //DT   <- 在 /*autoarg*/ 列表中始终保留
    /*autoarg*/);
```

说明：

- 模块**端口声明本身**（`input`/`output`/`inout` 行）从不被 AD 重写，
  所以那里不需要 `//DT`；它保护的是端口的那份手写 `wire`/`reg` 伴随
  声明；
- 没有 `//DT` 时（AD 范围）：可证明过期的位宽就地增长；*冲突*的
  （不可证明过期，如符号位宽不同）保留原样但标注 `//AD_CONFLICT:`，
  由你手工对账；
- `verilog-autodef-user-boundary: nil` 关闭 AD 范围的这套机制：AD 转而
  自由重写手写声明。

### 3. 文件局部变量 —— `// verilog-xxxx: value`

设置从**缓冲区内部的注释**读取：行内注释（文件任意位置的
`// verilog-auto-inst-param-value:t`）或文件末尾经典的
`// Local Variables:` 块。格式与 emacs verilog-mode 相同，文件可在两个
工具间往返。

支持的变量（全部可选；布尔默认值**粗体**）：

| 变量 | 取值（默认） | 控制什么 |
|---|---|---|
| `verilog-auto-ignore-concat` | bool（**开**） | 上面的 `{}`/`()` 豁免 —— AUTOINPUT/OUTPUT/INOUT 候选扫描跳过这类连接 |
| `verilog-auto-input-ignore-regexp` / `verilog-auto-output-ignore-regexp` / `verilog-auto-inout-ignore-regexp` | elisp/Python 正则 | 从生成端口中排除的名字模式 |
| `verilog-auto-wire-comment` | bool（**开**） | `nil` 时不生成声明上的 `// To u_…` / `// From u_…` 注释 |
| `verilog-auto-wire-type` | string | AW/AREG 声明的网络类型（如 `logic`） |
| `verilog-auto-declare-nettype` | string | AUTOINPUT/AUTOOUTPUT 端口声明的网络类型（如 `logic`）；也让无数据类型的 io 声明取 `<方向> <nettype>`（为 `` `default_nettype none `` ） |
| `verilog-autodef-user-boundary` | bool（**开**） | 手写声明归用户所有：冲突时标注 `//AD_CONFLICT:` 而非重写 |
| `verilog-auto-inst-param-value` | bool（关） | 把 `#(...)` 参数值代入生成的端口范围（`[AW-1:0]` → `[7:0]`）；不开则参数保持符号形式 |
| `verilog-auto-inst-vector` | bool（**开**） | 默认 AUTOINST 连接的总线下标；`nil` 时若父模块已按匹配位宽声明该 net 则省略下标，`unsigned` 只给 unsigned 端口加下标 |
| `verilog-auto-inst-sort` | bool（关） | 在每个方向组内对引脚排序 |
| `verilog-auto-inst-dot-name` | bool（关） | 生成 SystemVerilog `.name` 简写连接 |
| `verilog-auto-inst-templated-comment` | bool（**开**） | `nil` 时去掉模板引脚上的 `// Templated` 标记 |
| `verilog-auto-inst-template-required` | bool（关） | 非 nil 时省略没有任何 AUTO_TEMPLATE 条目匹配的端口 |
| `verilog-auto-simplify-expressions` | bool（**开**） | `nil` 时位宽表达式原样保留、不折叠常数 |
| `verilog-auto-arg-sort` | bool（关） | AUTOARG 端口名排序而非按声明顺序 |
| `verilog-auto-arg-format` | `packed` | `single` 时 AUTOARG 每行一个端口 |
| `verilog-library-directories` | `("-y" "dir" …)` | 模块搜索路径（相当于 `-y` 命令行标志） |
| `verilog-library-files` | `("file" …)` | 额外的模块库文件 |
| `verilog-library-flags` | `("-f" "../filelist.vc")` | 其目录加入搜索路径的 filelist |
| `verilog-auto-read-includes` | bool（**开**） | 一并解析 `` `include`` 文件中的参数/`` `define``/接口 |
| `verilog-typedef-regexp` | regexp | 匹配的名字是类型、永远不是 net（AW/AD 声明跳过） |
| `verilog-align-typedef-words` | words | ADF 像对齐 `reg`/`wire` 一样对齐的额外类型词 |
| `verilog-auto-python-file` | filename | 供 `/*AUTO_PYTHON*/` 模板表达式使用的 Python 模块 |
| `verilog-active-low-regexp` | regexp | 低有效复位名（AUTOSENSE/AUTORESET） |
| `verilog-assignment-delay` | string | AUTORESET 赋值里写的延迟 |
| `verilog-auto-reset-widths` | 见 AUTORESET | AUTORESET 赋值的位宽策略 |
| `verilog-auto-reset-blocking-in-non` | bool | 非复位分支用阻塞赋值 |
| `verilog-auto-sense-defines-constant` | bool | AUTOSENSE 把 `` `define`` 当常数 |
| `verilog-auto-sense-include-inputs` | bool | AUTOSENSE 包含只进不出的信号 |
| `verilog-auto-tieoff-declaration` | string | AUTOTIEOFF wire 的声明风格 |
| `verilog-auto-tieoff-ignore-regexp` | regexp | AUTOTIEOFF 不动的信号 |
| `verilog-auto-unused-ignore-regexp` | regexp | AUTOUNUSED 不动的信号 |

布尔值遵循 emacs 约定：`:nil` 关闭，其他任何值（`:t`、`:T`…）开启；
匹配不区分大小写。

## 快速上手

把 AUTO 标记放在你想生成内容的位置，然后按 `<leader>a`（leader 键见
[配置](#配置)），或运行 `:AALL`：

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

（默认连接就是端口名本身、带范围——不会凭空发明后缀）

重复运行是幂等的；`KI`/`KAR`/`KADT` 把区域折叠回标记。

## 命令一览

| 命令 | 作用 | 按键 |
|---|---|---|
| `AALL` | 一次跑完整个 AUTO 集合，顺序同 emacs `verilog-batch-auto`（见下） | `<leader>a` |
| `EAI` / `EAP` | verilog-mode 的 AUTOINST / AUTOINSTPARAM | `<leader>eai` `eap` |
| `EA` / `ED` | **emacs 后端**：对缓冲区运行原版 emacs `verilog-batch-auto` / `verilog-batch-delete-auto`，再用 `AF` 后格式化（取代旧 emacsauto.vim 的 `FixInstFormat`/`AutoInstFormat` 步骤） | `<leader>o` |
| `AIT` | 已废弃：委托给 `EAI`（verilog-mode AUTOINST 展开；stderr 有警告） | `<leader>ait` |
| `AIU` / `AIU1` | 最小差异的实例更新（保留手工连接） | `<leader>aiu` `aiu1` |
| `KI` | 把实例折叠回 `/*autoinst*/` 空壳 | `<leader>d` |
| `AD` / `ADT` | 再生成 `/*autodef*/` 的 wire/reg/integer/genvar 声明 | `<leader>ad` `adt` |
| `KADT` | 折叠 `/*autodef*/` 区域 | |
| `AR` | 再生成 `/*autoarg*/` 模块头端口表 | `<leader>ar` |
| `KAR` | 折叠 `/*autoarg*/` 区域 | |
| `AW` / `AREG` | AUTOWIRE / AUTOREG | `<leader>aw` `arg` |
| `AIO` | AUTOOUTPUT + AUTOINPUT + AUTOINOUT（wrapper 端口生成） | `<leader>aio` |
| `ASEN` | AUTOSENSE / AS —— 由 always 块实际读取的信号生成敏感表 | `<leader>as` |
| `ARST` | AUTORESET —— 为复位块里驱动的每个信号生成复位赋值 | `<leader>arst` |
| `AIM` / `AIC` / `AII` | AUTOINOUTMODULE / AUTOINOUTCOMP / AUTOINOUTIN —— 从另一个模块复制 I/O | |
| `AIMP` / `AIP` | AUTOINOUTMODPORT / AUTOINOUTPARAM —— 从接口 modport 复制 I/O / 从模块复制参数 | |
| `AAMP` | AUTOASSIGNMODPORT —— 生成连到接口 modport 的 assign | |
| `AOE` | AUTOOUTPUTEVERY —— 每个信号都成 output | |
| `ARI` | AUTOREGINPUT —— 为未声明的 AUTOINST 输入 net 声明 `reg` | |
| `AASC` | AUTOASCIIENUM —— 为枚举状态向量生成 ASCII 解码寄存器 | |
| `ALGC` | AUTOLOGIC —— 用 `logic` 声明的 AUTOWIRE | |
| `ATIE` | AUTOTIEOFF —— 把未端接的 output 拉到无效电平 | `<leader>atie` |
| `AUNU` | AUTOUNUSED —— 未用 input/inout 的逗号列表（给 `_unused_ok` 用） | |
| `AUND` | AUTOUNDEF —— `` `undef `` 本文件每个 `` `define `` | |
| `AIL` / `AILL` | AUTOINSERTLISP / AUTOINSERTLAST —— 插入 shell 命令输出（`!cmd`） | |
| `AINJ` | 给 legacy 代码注入 AUTO 标记，再跑 AALL（即 `verilog-inject-auto` 工作流） | `<leader>ainj` |
| `ADIF` | 当前缓冲区与完整 AUTO 展开的 diff（预览窗口；同 `verilog-diff-auto`） | `<leader>adif` |
| `ATLINT` | 警告未使用的 AUTO_TEMPLATE 行（quickfix；`verilog-auto-template-warn-unused`） | |
| `AF` | 格式化：端口、wire/reg、parameter/localparam、实例 | `<leader>af` |
| `AIF` `APF` `ADF` | 单个格式化通道 | `<leader>aif` `apf` `adf` |
| `AM` `AME` | 由光标下单词生成实例空壳 | `<leader>am` `ame` |
| `APM` `AFM` | `/*autopara*/` / `/*autofsm*/` 展开 | |
| `AH` / `ATpl {file}` | 文件头 / 按模板建新文件 | |
| `BPN` `BP` `BA` | always 块片段 | `<leader>bpn` `bp` `ba` |

EAI/AIT/AIU/AIU1 前的 `{count}` 选择第 N 个 `/*autoinst*/` 实例
（`:1AIT` → 只处理第一个）。没有 count 时，AIT/AIU/AIU1 处理光标下的
实例——前端会传 `--line` 带上光标行号——EAI/EAP/KI 则处理全部实例。
命令行上 `--which N`（0 起的标记序号）或 `--line N`（1 起的编辑器行号，
仅 AIT/AIU/AIU1）选择单个实例。

每条命令都会汇报做了什么，例如
`[verilog_tooling] eai: line 515: 1005 -> 937 line(s)`（或 `no changes`）。

### 性能与 `include` 处理

`AALL` 在**单个 Python 进程**里跑完整个管线，所有通道共享模块表；
模块文件通过缓存的目录清单定位、线程池读取——真实项目（594 个 `-y`
目录、2200+ 文件）上 `-a` 约 4 秒。

每个分析都会读穿 `` `include "x"``（参数、`` `define``、接口——包括
行内的 ``#(`include "…")`` 参数表形式），常数表达式（`A+B`、`$clog2(X)`、
`(X==1) ? 1 : $clog2(X)`）折叠为整数。文件缺失时指令原样保留（一条
警告）；输出文本始终保留原始 `` `include`` 行。这有意比 emacs 更强：
verilog-mode 只在被要求时读 include，且只读 `` `define``。
用 `// verilog-auto-read-includes:nil` 关闭。

仓库自带标准多实例 benchmark：`tests/bench_aall_perf.py`（400 个
带 packed/unpacked/参数化端口的实例跑完整 `aall` 流水线；`--emacs`
可同时计时真实的 verilog-mode 作为参照，`--verbose` 保留分步日志）：

```bash
python tests/bench_aall_perf.py --emacs
#   python 中位数: ~13s        emacs verilog-auto: ~0.5s
```

不进 pytest 门禁（太慢）——做性能相关改动前后跑一下即可。分步日志
能看清时间花在哪：多实例文件上 `AUTOWIRE/AUTOREG/autodef` 占大头。

## 重点命令

下面六条是日常主力，完整介绍。其余命令在上表中都是一行条目——这是
有意的：标记语义遵循 verilog-mode / automatic.vim。

### AALL —— 按正确顺序跑全部

`<leader>a`（即命令行的 `-a` 管线）在单个 Python 进程里按 emacs
`verilog-batch-auto` 的顺序跑完整个 AUTO 集合：

AIL → EAP → EAI → AASC → AIMP → AIM → AIC → AII → AIP → AIO → ATIE →
AUND → AAMP → ALGC → AW → AREG → ARI → AOE → ASEN → ARST → AUNU → AD →
AR → AILL → AF

大多数文件需要的就是这个：标记放好，按 `\a`，完事。没用到的标记是
no-op，输出与按此顺序逐条运行逐字节相同（模块文件只解析、读取一次，
速度正来自于此）。

### EA / ED —— emacs 后端

对缓冲区运行**原版** emacs `verilog-batch-auto`（EA）/
`verilog-batch-delete-auto`（ED），再用 `AF` 后格式化。这是旧
emacsauto.vim 工作流（`FixInstFormat`/`AutoInstFormat`）的直接替代：
结果与亲自跑 emacs 相同，不用离开 Vim。想用 verilog-mode 本身作引擎
时用它；想要本工具的引擎（异步、无 emacs 进程、尊重 `{}` 豁免与
`//DT`）时用 AALL。

### AIU / AIU1 —— 最小差异更新

日常「子模块变了」的工具：

- `AIU1` 保留每一行原样，把新端口追加到 `);` 之前（标 `// INST_NEW`），
  删除的端口注释掉（标 `// INST_DEL`）。
- `AIU` 做同样的更新，但按模块端口顺序重写实例，存活的连接行逐字
  复用——手工改过的 `.port (custom_sig)` 连接保持原文本。
  `AUTO_TEMPLATE` 条目对它声明的端口仍然优先。

例：子模块变了：`done` 被删，`dout`/`cnt` 是新增。从：

```verilog
module top;
    sub u_sub (/*autoinst*/
        .clk (rx_clk),
        .din (din),
        .done(done)
    );
endmodule
```

`AIU1`（每行原地保留；`AIU` 给出同样的更新、但按模块端口顺序重写）：

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

手写的 `.clk (rx_clk)` 连接两者都保留；时间戳是运行时刻（命令行
`--date` 可覆盖）。

### AD / ADT —— autodef：声明所有未声明的信号

`/*autodef*/` 是你不再手写声明的办法。把标记放在模块体内；AD 把区域
再生成到固定分段里，位宽由驱动者推断（字面量、部分选择、比较、信号
链、子模块端口位宽、`+:`/`-:` 索引部分选择）。位宽引用参数/`` `define``
时保持符号形式。`KADT` 把区域折叠回标记。

```
// Define io wire here            没有显式 wire/reg 的端口
// Define flip-flop registers here    时钟 always 里 <= 的左值
// Define combination registers here   其他 always 里 = 的左值
// Define wires here                  assign 的左值
// Define inst wires here             实例输出驱动的 net
// Define integer here                for 循环变量
// Unresolved define signals here     推断不出（以 // 注释形式）
// End of automatic define
```

类型规则：

- `always` 块左值 → `reg`；`assign` / 实例驱动 → `wire`
- **未驱动的 `output` → `reg`**（你接下来会从 always 块驱动它；与 AREG
  一致）。input/inout 与被驱动的 output 保持 `wire`
- **for 循环变量自动声明**：`generate` 循环里是 `genvar`，过程循环
  （always/function/task）里是 `integer`，包括两层嵌套；`mem[i][j]`、
  `fifo[i*W +: W]` 这类循环索引左值贡献未打包维度与增长后的位宽
- 手写声明保留（可证明过期的数字位宽就地增长）；加 `//DT`（"别动"）
  豁免一条——见 [`//DT`](#2-dt--不要动我手写的声明)
- SystemVerilog `logic` 处处可理解

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

变成（未声明的 `q` 落进触发器段；空段保持为注释头）：

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

#### 区域外的用户声明（`verilog-autodef-user-boundary`）

`/*autodef*/` 区域之外的声明归用户所有。当推断出的位宽/维度与这样
一条声明**冲突**时，默认（文件局部 `// verilog-autodef-user-boundary:t`）
保留该行并在行尾追加一条注明推断形状的注释：

```verilog
wire [35:0] ram_rdata;  // my decl  //AD_CONFLICT: infer wire[1:0][35:0]
```

注释是幂等的（重跑不重复追加），声明手工改好后消失，文本跟随当前
推断。带 `//DT` 的声明完全豁免（不重写、不标注）；推断不出位宽/维度
时不标注。`// verilog-autodef-user-boundary:nil` 恢复旧行为：可证明
错误的声明就地修正（缺失或错误的打包维度、可证明过期的位宽）。

### AR —— 模块头端口表

从端口声明再生成模块头里的 `/*autoarg*/`：`//Inputs` / `//Outputs` /
`//Inouts` 分段，名字 4 空格缩进打包、超过第 40 列换行。处理头风格
声明（`input clk,`）、一行多声明（`input a, input b`）、跨行声明与
未打包维度（`val[3:0]`）。放错位置的标记（不在模块头里）原样不动。
`KAR` 把列表折叠回标记。分段顺序是 Outputs、Inouts、Inputs——emacs
的顺序。

```verilog
module top (/*AUTOARG*/);
    input  wire       clk;
    input  wire [7:0] din;
    output wire [7:0] dout;
endmodule
```

变成：

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

带 `//DT` 的手写头端口在重建后始终保留——见
[`//DT`](#2-dt--不要动我手写的声明)。

**Inout 推断**：端口表里的名字若只有一条 `wire` 声明、或完全没有方向，
它不是合法的 Verilog 端口，会被丢弃（逐名警告）——除非它连到某个例化
模块的 **inout 引脚**（如 pad：`.GPIO0_A00 (GPIO0_A00)`）。这时 AR 把它
保留在 `//Inouts` 段，并生成一条 `inout wire name;` 的体内声明让端口
合法——AUTOWIRE/autodef 也永不给它声明 `wire`。其他无方向的名字仍然
丢弃，逐个列在 stderr / `:messages` 里。

### AF 家族 —— 对齐

缓冲区本地格式化（不需要模块文件）：

- `APF` 对齐端口声明（`input`/`output`/`inout`），
- `ADF` 对齐 `wire`/`reg`/`logic` 声明与 `parameter`/`localparam`
  （`=` 对齐），
- `AIF` 对齐实例端口连接，
- `AF` 三者全跑。

幂等；已对齐时汇报 `no changes`。实例头里手写的 `#(...)` 参数覆盖
与引脚连接完全同样对齐——只有 `)) inst (` 收尾行从不折进最后一个
覆盖项。

## 格式与模块查找

我们自己的格式与约定：库目录怎么写、模板怎么写、Python 辅助文件
怎么写。

### 模块文件如何查找 —— dir 的写法

EAI/AIT/AW 需要子模块的源文件来读它的端口。搜索顺序：

1. 缓冲区 Local Variables 块里的 `verilog-library-directories` /
   `verilog-library-flags`（相对路径相对于**真实文件的目录**解析，
   `~` 与开头的 `$VAR` 会展开）
2. 其中列出的 `-f file.vc` 库文件（递归）
3. 正在编辑文件所在的目录，以及同一缓冲区内定义的模块
4. `g:verilog_tooling_libdirs` / 命令行 `-y` 目录

dir 写在文件底部的 Local Variables 块里：

```verilog
// Local Variables:
// verilog-library-directories:("../pmu" "../por_rst" "../../common/clk/rtl/com")
// verilog-library-flags:("-f ../mm_file_dir.vc")
// verilog-auto-inst-param-value:t
// End:
```

它指向的 `.vc` 文件（每行一条；`#`/`//` 注释可）：

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
`+libext+.v+.sv…`。模块找不到时，它的实例被跳过并给出
`[verilog_tooling]` 警告，列出搜索过的目录。

### AUTO_TEMPLATE (EAI/EAP)

模板按实例定制连接。它们位于实例上方的注释里（或 `verilog-inst-file:`
命名的文件中）：

```verilog
/* mm_cdma_parse AUTO_TEMPLATE (
    .CDMA_REQ_NUM        (CDMA_INTERNAL_NUM),
    .cmd_apb_fifo_rdata  (cmd_apb_fifo_rdata_r[]),
    .cdma_\(ar\|aw\)id_s (cdma_\1id_internal[1:0]),
    .int_req             (int_req_@),
) */
mm_cdma_parse u_parse (/*autoinst*/);
```

- **连接**里的 `@` 展开为实例号（实例名的第一组数字；用
  `AUTO_TEMPLATE "my_re_\([0-9]+\)"` 覆盖 `@` 的匹配对象）
- **端口模式**里的 `@` 匹配一组数字：`.port_@ (sig_@)` 覆盖
  `port_0 … port_3`
- `[]` 展开为所连端口声明的打包范围（`rdata[7:0]`；标量端口时去掉）
- 正则端口模式里的 `\(...\)` 分组以 `\1`、`\2`… 回代
- **Emacs 与 Python 两种正则方言都支持——自动识别**（此处以及
  `AUTO_TEMPLATE "regexp"`、`/*AUTOINST("regex")*/` 过滤器、
  `verilog-typedef-regexp`）：Emacs 的 `\(...\)`/`\|` 与 Python 的
  `(...)`/`|` 甚至可以混用；Python 风格的 `\g<1>` 回引与 `\1` 并行
  可用
- `@"expr"` 求值一个表达式——Python（`@"'pre_%d' % @"`），或带括号的
  elisp（`@"(downcase vl-name)"`；支持的形式：`substring`、`downcase`、
  `concat`、`if`、`equal`、算术、`let`、`setq`；绑定 `vl-name`、
  `vl-cell-name`、`vl-width`、`vl-dir`）。Python 表达式里带连字符的
  名字（`vl-width`）会自动改写为下划线别名（`vl_width`），因为连字符
  会被当作减法。`vl-width`/`vl_width` 是*数字*端口宽度（`[0:3]` 为
  `'4'`，单比特为 `'1'`，参数化范围为 `'(1+(`a)-(`b))'`）——与 emacs
  的 `verilog-sig-width` 一致。
- `/*AUTO_LISP(...)*/` 与 `/*AUTO_PYTHON(...)*/` 为 `@"..."` 表达式
  准备名字——见下文 [AUTO_PYTHON 与 AUTO_LISP](#auto_python-与-auto_lisp)。

没有模板条目时，端口连到同名 net（带范围）；net 尚未声明时，AW/AD
会替你声明。SystemVerilog `interface` 端口（带 modport）受支持：
`cpu_bus.master bus` 连为 `.bus (bus.master)`。星号展开开启时 `.*`
实例按 AUTOINST 展开。

两个值得知道的 EAI 行为：

- EAI 是**全量重置**（emacs 语义）：模板驱动的引脚标 `// Templated`；
  在标记行**之前**已连接好的引脚会被保留。想原地保留手写连接，用
  AIU/AIU1。
- **维度注释是真信息。** 多维端口的连接带一条 `net/*[D1][D2]*/` 注释
  记录端口的声明形状（还有未打包维度时是 `net/*[D1].[U1]*/`）：后续
  通道都把这条注释当作权威形状（注释 > 端口声明 > 其他证据）。元素/
  切片连接（`net[0][35:0]`）自带信息、不加注释。不要手工删注释；
  `kill` 类命令清区域内容但从不动连接注释，AIU/AIU1 原样保留已有
  注释。

AUTOINSTPARAM（EAP）按此优先级填充 `#(...)` 参数表：该参数的
`AUTO_TEMPLATE` 条目；父模块的同名 parameter/localparam（符号透传）；
子模块的默认值（当它在父模块可见时）；否则省略该参数。

### AUTO_PYTHON 与 AUTO_LISP

模板 `@"..."` 表达式常常需要内置变量之外的东西——名字变换、常数
后缀、字符串拼接。两个注释块为它们准备 Python 名字。两者都按缓冲区
顺序求值、只到正在展开的实例为止，在**无 builtins** 的沙箱里运行
（不能 `import`、不能访问文件或网络）；块失败会中止命令并引用出错
的代码。

- `/*AUTO_LISP(...)*/` 执行 Python 语句（赋值），其名字可供后面的
  `@"..."` 表达式读取——是 elisp 的 `AUTO_LISP` / `verilog-auto-lisp`
  准备步骤的移植，用 Python 语法代替 elisp `setq`。
- `/*AUTO_PYTHON( <code> )*/` 运行完整的 Python 语句，通常是 `def`；
  定义的名字可被后续任何 `@"..."` 表达式调用：

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

把 `.din` 连到 `{din,din}`。在 `@"..."` 与 AUTO_PYTHON 代码里，模板
变量同时有 elisp 拼写（`vl-name`、`vl-cell-name`、`vl-width`、
`vl-dir`）与下划线别名（`vl_name` 等）。

多文件共享的辅助函数可以放在真正的 `.py` 文件里——见下一节。

### `verilog-auto-python-file` —— Python 辅助文件怎么写

不必把同一段 `/*AUTO_PYTHON*/` 块粘进每个缓冲区：把辅助函数放进
真正的 `.py` 文件，用文件局部变量点名：

```verilog
// Local Variables:
// verilog-auto-python-file: "funcs.py"
// End:
```

（与 `verilog-library-files` 相同的带引号、空白分隔语法；括号可选，
后面的文件覆盖前面的）。`funcs.py` 在库路径上时：

```python
def shout(sig):
    return sig.upper() + "_INT"
```

模板像调用行内定义一样调用它——模板 `.din (@"shout(vl_name)")` 把
`din` 连到 `DIN_INT`。

查找与优先级：

- 先在 `-y`/vc 库目录里搜，然后是缓冲区自己的目录（`~` 与开头的
  `$VAR` 会展开）；
- 定义按文件 mtime 缓存——跨运行不会用到过期辅助函数；
- 文件局部的先加载，其次是行内的 `/*AUTO_PYTHON*/` 块；同名冲突时
  后定义者胜（行内胜过文件）；
- 一切都在同一个无 builtins 沙箱下运行，所以辅助文件可以定义函数和
  常量，但碰不到文件系统。

## 默认按键映射

插件在 `VimEnter` 时安装这些 normal 模式 leader 映射——所以 vimrc 里
任何位置设置的 `mapleader` 都被尊重——且只在该 lhs 尚无映射时安装，
你自己的映射永远优先：

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

用 `let g:verilog_tooling_no_mappings = 1` 关闭整套映射。

## 插入缩写（可选，默认关闭）

`plugin/verilog_abbrev.vim` 移植 automatic.vim 的全局插入缩写，只作用
于 `*.v`/`*.vh`/`*.sv`/`*.svh` 缓冲区：`<=` → `` <= #`RD ``、
`beg` → `begin`、`begni`（拼写错误）→ `begin`。在 vimrc 里用
`let g:verilog_abbrev_enable = 1` 开启。


## 命令行

插件做的每件事也是一个命令行程序——Vim 前端就是调用这些入口（只用
标准库）。把 `PYTHONPATH` 指向仓库的 `python/` 目录（或已安装的
`~/.vim/python`）：

| 程序 | 命令 |
|---|---|
| `python3 -m verilog_tooling.inst` | `aall eai eap ait aiu aiu1 kill aif apf adf af ainj` |
| `python3 -m verilog_tooling.arg` | `ar kill` (AUTOARG / KAR) |
| `python3 -m verilog_tooling.autodef` | `adt kill` (AD/ADT / KADT) |
| `python3 -m verilog_tooling.wire` | `aw ar kill-aw kill-ar` (AUTOWIRE / AUTOREG) |
| `python3 -m verilog_tooling.inout` | `aio ain aout ainout kill-ain kill-aout kill-ainout` |
| `python3 -m verilog_tooling.sense` | `asense areset kill-sense kill-reset` |
| `python3 -m verilog_tooling.misc` | `aascii alogic atieoff aunused aundef ainsertlisp ainsertlast` (+ `kill-*`) |
| `python3 -m verilog_tooling.xfer` | `ainoutmodule ainoutcomp ainoutin ainoutmodport ainoutparam aassignmodport aoutputevery areginput` (+ `kill-*`) |
| `python3 -m verilog_tooling.gen` | `am ame apm afm kill-para kill-fsm` (`am`/`ame` 接受 `--line N`) |
| `python3 -m verilog_tooling.filehdr` | `template header` (`template -f new.v` 创建文件) |
| `python3 -m verilog_tooling.inject` | `inject`（单独的 AINJ 标记通道） |
| `python3 -m verilog_tooling.diffauto` | `diff`（统一 diff 到 stdout，或 `-o file`） |
| `python3 -m verilog_tooling.lint` | `lint`（未用 AUTO_TEMPLATE 警告到 stdout） |

通用标志：`-i in.v -o out.v`（输入/输出文件）、`-y dir`（库目录，可
重复）、`-I name`（额外的 SystemVerilog 接口类型名）、`--ref_file f`
（草稿缓冲区背后的真实文件，供 Local-Variables 相对路径用）。
`verilog_tooling.inst` 另有：

| 标志 | 作用 |
|---|---|
| `--which N` | 只处理第 N 个 `/*AUTOINST*/` 标记（0 起） |
| `--line N` | 只处理编辑器第 N 行的实例（仅 AIT/AIU/AIU1） |
| `--sort` | EAI/EAP 在每个方向组内排序引脚 |
| `--dot-name` | 生成 SystemVerilog `.name` 简写连接 |
| `--param-value` | 把 `#(...)` 参数值代入端口位宽 |
| `--star-expand` / `--star-save` | 展开 `.*` 实例 / 保留带标记的展开 |
| `--date S` | 覆盖 INST_NEW/INST_DEL 时间戳 |

```bash
PYTHONPATH=python python3 -m verilog_tooling.inst eai -i top.v -o top.out.v -y rtl -y ip
```

## 目录结构

```
plugin/verilog_tooling.vim   Vim 前端（自动加载）
plugin/automatic.vim         头/波形片段（BPN/BP/BA、AddClk/AddSig/AddBus）
plugin/verilog_abbrev.vim    可选的缓冲区局部 iabbrev，作用于 .v/.vh/.sv/.svh（默认关）
python/verilog_tooling/      Python 包（只用标准库）
doc/verilog_tooling.txt      vim help
install.sh                   一键安装 / 环境检查
```

Vim 设置（`g:verilog_tooling_*`）：见[配置](#配置)。

### 信号维度追踪（`VERILOG_TOOLING_TRACE`）

当某个 net 的位宽出错，追踪它的维度从哪来：把 `VERILOG_TOOLING_TRACE`
设为逗号分隔的信号名列表（区分大小写；`*` 追踪全部），这些信号的
AUTOWIRE/AUTODEF 决策逐条事件写到 stderr（`first-driven`、`conn-dims`、
`skip-declared`、`emit-decl`、`update-width`/`update-dims`、`dt-exempt`、
`multi-driver` 等）：

```bash
VERILOG_TOOLING_TRACE=ram_rdata python3 -m verilog_tooling.inst aall -i a.v -o out.v -y .
# [vt-trace] ram_rdata: first-driven inst=u0_b module=b width=c0 pdims=['1:0', '35:0']
```

追踪是调试开关，**不**被 `VERILOG_TOOLING_QUIET` 抑制。
