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

按文件生效的设置（`verilog-auto-*` / `verilog-library-*` 变量）写在
缓冲区本身里——见[核心概念 §3](#3-文件局部变量--verilog-xxxx-value)。

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

把 AUTO 标记放在你想生成内容的位置，然后按 `<leader>a`（默认 leader
即 `\a`；`let mapleader = "-"` 时为 `-a`），或运行 `:AALL`：

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
| `AINJ` | 给 legacy 代码注入 AUTO 标记，再跑 AALL | `<leader>ainj` |
| `ADIF` | 当前缓冲区与完整 AUTO 展开的 diff（预览窗口） | `<leader>adif` |
| `ATLINT` | 警告未使用的 AUTO_TEMPLATE 行（quickfix） | |
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

## 命令详解

### AALL —— 按正确顺序跑全部

在单个 Python 进程里运行 emacs `verilog-batch-auto` 的序列：

AIL → EAP → EAI → AASC → AIMP → AIM → AIC → AII → AIP → AIO → ATIE →
AUND → AAMP → ALGC → AW → AREG → ARI → AOE → ASEN → ARST → AUNU → AD →
AR → AILL → AF

输出与按此顺序逐条运行逐字节相同，但模块文件只解析、读取一次。
`g:verilog_tooling_eai_flags`（如 `--sort`）会被尊重。没用到的标记是
no-op，所以 AALL 对任何缓冲区都安全。

### EAI —— verilog-mode 的 AUTOINST

展开 `/*AUTOINST*/`：丢弃上一次展开，按声明顺序把子模块的每个引脚
连好，分组为 `// Interfaces` / `// Outputs` / `// Inouts` / `// Inputs`
（`--sort` 在组内排序）。默认连接是端口名带范围——`.dout (dout[7:0])`。
说明：

- EAI 是**全量重置**（emacs 语义）：想保留自定义连接就写进
  `AUTO_TEMPLATE`——模板驱动的引脚标 `// Templated`。想原地保留手写
  连接，用 AIU/AIU1。
- 在标记行**之前**已经连接好的引脚会被保留。
- `/*AUTOINST("regex")*/` 只保留匹配的引脚；`?!` 前缀表示排除
  （不区分大小写；emacs 与 Python 两种正则方言都可）。
- SystemVerilog `.*` 实例在星号展开开启时展开；接口端口连为
  `.bus (bus.master)`。
- **维度注释是真信息。** 多维端口的连接带一条 `net/*[D1][D2]*/` 注释
  记录端口的声明形状（还有未打包维度时是 `net/*[D1].[U1]*/`）：连接
  文本本身承载不了端口维度，所以后续通道（AUTOWIRE/AUTODEF 的维度
  推导、模板 `[]`、AIF、再生成）都把这条注释当作权威形状（注释 >
  端口声明 > 其他证据）。元素/切片连接（`net[0][35:0]`）自带信息、
  不加注释。不要手工删注释；`kill` 类命令清区域内容但从不动连接
  注释，AIU/AIU1 原样保留已有注释。
- 找不到模块文件的实例会被跳过，并警告列出搜索过的目录。

### EAP —— AUTOINSTPARAM

按以下优先级从子模块的参数声明填充 `#(...)` 参数表：

1. 该参数的 `AUTO_TEMPLATE` 条目
2. 父模块的同名 parameter/localparam（符号透传——**不**换成数字，
   这样更上层例化时仍然成立）
3. 子模块的默认值（当它在父模块可见时）
4. 否则省略该参数（用子模块默认值）

```verilog
module subp #(
    parameter DW = 8,
    parameter DEPTH = 4
) (
    input  wire          clk,
    output wire [DW-1:0] dout
);
module top;
    subp #(/*AUTOINSTPARAM*/) u_subp (/*autoinst*/);
endmodule
```

变成：

```verilog
module top;
    subp #(/*AUTOINSTPARAM*/
           // Parameters
           .DW                          (DW),
           .DEPTH                       (DEPTH)) u_subp (/*autoinst*/);
endmodule
```

带 `verilog-auto-inst-param-value:t`（或命令行 `--param-value`）时，
实例 `#(...)` 的覆盖值还会代入端口位宽与模板连接（常数表达式与
`$clog2` 会折叠）。

### AIT —— 已废弃，委托给 EAI

`AIT` 原是 automatic.vim 的 AutoInst（杀掉当前连接、再生成同名连接）。
那会丢失手写连接（`.clk (rx_clk)` 变成 `.clk (clk)`），所以废弃：现在
它打印警告并委托给 emacs（EAI）展开——保留每个已有连接、只补缺失的。
Vim 的再生成引擎保留为内部实现（AIU 用它展开 `/*autoinst*/` 空壳）。
请改用 `EAI`。

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

### KI —— 折叠实例

删除生成的引脚表，只留 `mod inst (/*autoinst*/);` 空壳。下一次
EAI/AIT 从头重建：

```verilog
    sub u_sub (/*autoinst*/);
```

### AD / ADT —— 声明所有未声明的信号

把 `/*autodef*/` 区域再生成到固定分段里，位宽由驱动者推断（字面量、
部分选择、比较、信号链、子模块端口位宽、`+:`/`-:` 索引部分选择）。
位宽引用参数/`` `define`` 时保持符号形式。`KADT` 把区域折叠回标记。

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

### AW / AREG —— AUTOWIRE / AUTOREG

`/*AUTOWIRE*/` 为实例输出驱动的 net 声明 wire（带 `// From u_x of mod.v`
注释）；`/*AUTOREG*/` 为没有任何驱动者（assign/always/实例）的模块
output 声明 `reg`。两者都跳过已声明者，位宽符号化解析。

```verilog
    /*AUTOWIRE*/
    sub u_sub (/*autoinst*/
        .clk  (clk),
        .din  (din),
        .dout (dout_w),
        .done (done_w)
    );
```

变成：

```verilog
    /*AUTOWIRE*/
    // Beginning of automatic wires (for undeclared instantiated-module outputs)
    wire                                    done_w; // From u_sub of sub.v
    wire         [7:0]                      dout_w; // From u_sub of sub.v
    // End of automatics
```

### AIO —— AUTOINPUT / AUTOOUTPUT / AUTOINOUT

verilog-mode 的 wrapper 模块三件套（`:AIO` 依次跑 AUTOOUTPUT、
AUTOINPUT、AUTOINOUT，`AALL` 三者都包含）：

- `/*AUTOINPUT*/` 为每个喂给实例 input、且在模块内未声明也未被驱动
  的 net 声明 `input` 端口（`// To u_x of mod.v` 注释）；
- `/*AUTOOUTPUT*/` 为每个被实例 output 驱动、尚不是端口、也不喂给
  另一个实例的 net 声明 `output` 端口（喂给别的实例的留在内部——
  那是 AUTOWIRE 的地盘）；
- `/*AUTOINOUT*/` 为每个挂在实例 inout 上、尚不是端口的 net 声明
  `inout` 端口（`// To/From u_x of mod.v` 注释）。没有这个标记时，
  inout 连接的 net 保持内部 wire——AUTOWIRE 以 `// To/From` 注释
  声明它们。

放在模块头括号内时按 Verilog-2001 风格展开（逗号分隔，带 verilog-mode
的开关逗号修复）；在体内则是 Verilog-1995 风格（`;`）。每个标记可带
正则过滤——`/*AUTOINPUT("^i_")*/`，`?!` 前缀取反——文件局部的
`verilog-auto-*-ignore-regexp` 变量同样生效。位宽取自子模块端口并代入
实例的 `#(...)` 参数覆盖值。未声明的 assign 驱动 net 会被做成 input
（与 verilog-mode 一致——把它提升为端口、自己删掉 assign）；
concat/表达式连接被跳过，除非 `verilog-auto-ignore-concat` 为 nil
（见[核心概念](#1--豁免--让信号不上端口verilog-auto-ignore-concat)）。

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

变成：

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

AIO 与 `/*autoarg*/`：模块头有 `/*autoarg*/` 时，把 AUTOINPUT/AUTOOUTPUT
标记放在**体内**——autoarg 会把生成的端口名打包进模块头（verilog-mode
的标准布局）。AIO 标记本身就在模块头里时，autoarg 不动这个头（名字
列表会与完整声明重复）。

### ASEN / ARST —— AUTOSENSE / AUTORESET

`always @(/*AUTOSENSE*/)`（或 `/*AS*/`）按块实际读取的信号重写敏感表
——块内被赋值的信号被排除，`/*AUTO_CONSTANT(`x) */` 排除 `` `define``，
存储器得到 `/*memory or*/` 注记。复位分支里的 `/*AUTORESET*/` 生成
`// Beginning of autoreset for uninitialized flops`，为块内其他地方
驱动、但在标记之前没有手工复位的每个信号写 `sig <= width'h0;`
（`<=` 与 `=` 跟随块的风格；低有效名按 `verilog-active-low-regexp`
复位到 1）。

```verilog
    always @(/*AUTOSENSE*/) begin
        q = a & b;
    end
```

变成 `always @(/*AUTOSENSE*/a or b) begin …`；AUTORESET 同理为每个
未复位的触发器在复位分支填上 `q <= 8'h0;` 形式的赋值。

### AIM / AIC / AII / AIMP / AIP —— 从别处复制 I/O

- `/*AUTOINOUTMODULE("Mod"[,"re"])*/`（AIM）：从另一个模块复制
  input/output/inout 声明——空壳模块的主力。
- `/*AUTOINOUTCOMP("Mod"[,"re"[,"not-re"]])*/`（AIC）：同上但取补
  （input 变 output）——给 testbench 用。
- `/*AUTOINOUTIN("Mod"[,"re"])*/`（AII）：同上但全变 input——给
  monitor 用。
- `/*AUTOINOUTMODPORT("If","mp-re"[,"re"[,"prefix"]])*/`（AIMP）：从
  接口 modport 复制 I/O。
- `/*AUTOINOUTPARAM("Mod"[,"re"])*/`（AIP）：复制 `parameter` 声明
  （无值，SystemVerilog-2009 风格）。

在模块头内生成 Verilog-2001 逗号风格，否则 1995 的 `;` 声明。正则
加 `?!` 前缀表示排除匹配。

AIM 例——空壳模块加上 AIU 例中的 `sub` 定义：

```verilog
module shell;
/*AUTOINOUTMODULE("sub")*/
endmodule
```

变成：

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

### AAMP —— AUTOASSIGNMODPORT

`/*AUTOASSIGNMODPORT("If","mp-re","inst"[,"re"[,"prefix"]])*/` 生成
把 modport 信号与接口实例互连的 `assign` 语句——给 UVM 验证模块用。
接口 `bus_if`（modport `master`：`output req, data`、`input gnt`）、
实例 `bus_if u_bus ();` 时：

```verilog
    /*AUTOASSIGNMODPORT("bus_if", "master", "u_bus")*/
    // Beginning of automatic assignments from modport
    assign data = u_bus.data;
    assign req = u_bus.req;
    assign u_bus.gnt = gnt;
    // End of automatics
```

（modport 的 output 在前、input 在后，各自按名排序；方向关键字跨逗号
项延续，所以 `data` 也是 output。）

### AOE / ARI —— AUTOOUTPUTEVERY / AUTOREGINPUT

`/*AUTOOUTPUTEVERY[("re")]*/` 把每个非 input 信号声明为 output（防止
综合把信号优化掉）。`/*AUTOREGINPUT*/` 为喂给 AUTOINST 输入引脚的未
声明 net 声明 `reg`（`// To <inst> of <Mod>.v`），适合顶层测试壳。
在上文的 `sub u_sub` 实例上（内部 wire `dout_w`/`done_w`，未声明的
`clk`/`din` 喂给它）：

```verilog
    /*AUTOOUTPUTEVERY*/
    // Beginning of automatic outputs (every signal)
    output              done_w;
    output [7:0]        dout_w;
    // End of automatics

    /*AUTOREGINPUT*/
    // Beginning of automatic reg inputs (for undeclared instantiated-module inputs)
    reg                 clk;                    // To u_sub of sub.v
    reg [7:0]           din;                    // To u_sub of sub.v
    // End of automatics
```

### AASC —— AUTOASCIIENUM

`/*AUTOASCIIENUM("sig", "ascii_sig"[,"prefix"[,"onehot"]])*/` 为枚举
状态向量生成 ASCII 解码寄存器：带 `// auto enum <name>`（或
`synopsys enum`）标记的参数定义状态，带 `/* auto state_vector <sig> */`
标记的信号指明被解码的向量。生成 `reg [8*N-1:0] ascii_sig; // Decode of sig`
以及 `always @(sig)` 的 case 解码器（默认 `"%Err"`）。

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

变成：

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

（`SM_` 前缀从解码名中去掉，名字按最宽的状态名补空格对齐。）

### ALGC —— AUTOLOGIC

`/*AUTOLOGIC*/` 就是声明 `logic` 而非 `wire` 的 AUTOWIRE。文件局部
`// verilog-auto-wire-type: "logic"` 对 `/*AUTOWIRE*/` 有同样效果。

### ATIE —— AUTOTIEOFF

`/*AUTOTIEOFF*/` 把每个未端接的模块 output 拉到无效值：
`wire [w:0] o = w'h0;`（低有效名用 `~w'h0`，signed 用 `w'sh0`）。
已声明、被 AUTOINST 驱动、或匹配 `verilog-auto-tieoff-ignore-regexp`
的 output 跳过。是 `AUNU` 的经典空壳模块搭档。

```verilog
    /*AUTOTIEOFF*/
    // Beginning of automatic tieoffs (for this module's unterminated outputs)
    wire [7:0]          o_data                  = 8'h0;
    wire                o_vld_n                 = ~1'h0;
    // End of automatics
```

（空壳模块的 `o_vld_n` 按文件局部 `verilog-active-low-regexp: "_n$"`
以 `~1'h0` 拉到 1。）

### AUNU —— AUTOUNUSED

`/*AUTOUNUSED*/` 就地展开为未使用的 input/inout 信号逗号列表——为
`wire _unused_ok = &{1'b0, /*AUTOUNUSED*/ 1'b0};` 而设计，一个 pragma
消掉所有未用警告。`verilog-auto-unused-ignore-regexp` 排除名字。

"未使用"指*没有连到任何实例的 input/inout 引脚*（`{...}`/`(...)` 里的
连接算已使用）；被 `assign` 读取救不了它。未用输入 `spare_a`/`spare_b`
展开为：

```verilog
    wire unused_ok = &{1'b0,
                       /*AUTOUNUSED*/
                       // Beginning of automatic unused inputs
                       spare_a,
                       spare_b,
                       // End of automatics
                       1'b0};
```

### AUND —— AUTOUNDEF

`/*AUTOUNDEF[("re")]*/` 为上一个 AUTOUNDEF 以来见到的每个 `` `define ``
生成 `` `undef ``（已 undef 的名字跳过，所以 `` `ifdef NEVER `` 守卫
成立），排序，可选正则过滤——让文件局部 define 不污染全局命名空间。

### AIL / AILL —— AUTOINSERTLISP / AUTOINSERTLAST

`/*AUTOINSERTLISP(!command args)*/` 运行 shell 命令并把其 stdout 插入
`// Beginning of automatic insert lisp` 区域，在所有其他 AUTO 之前
（AIL）或之后（AILL）。Emacs 在这里求值 elisp；本移植运行 shell——这是
有据可查的差异（不支持 elisp `defun`，与 AUTO_TEMPLATE 的 `@"..."`
子集规则一致）。

### AINJ —— 给 legacy 代码注入 AUTO 标记

把 `/*AUTOARG*/` 注入模块头、把 `/*AS*/` 注入手写敏感表已经吻合的
always 块、把 `/*AUTOINST*/` 注入引脚表（删除 `.x(x)` 同名引脚），
然后跑完整的 AALL 管线——即 `verilog-inject-auto` 的工作流，让旧文件
进入 AUTO 管理。（`/*AS*/` 标记只在既有手写敏感表已与块的读取一致时
才注入——否则 always 块原样留给你先修。）

### ADIF —— 对比 AUTO 展开差异

在副本上展开 AUTO 并在预览窗口显示统一 diff（空白不敏感的检测，同
`verilog-diff-auto`）。输出为空意味着缓冲区已完全展开——适合做
lint/回归检查。

### ATLINT —— 未使用的 AUTO_TEMPLATE 行

带命中追踪地运行 EAI/EAP 展开，把从未被任何实例消费的模板条目列进
quickfix 窗口（`verilog-auto-template-warn-unused`）：

```
top.v:2: AUTO_TEMPLATE line unused: ".nosuch (nosuch)"
```

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

### AM / AME —— 实例空壳

把光标下的单词变成该行上的实例空壳。实例序号数的是本模块在缓冲区
里已有的实例数。光标下的 `fifo` 用 `AM`（automatic.vim 风格）变成：

```verilog
fifo  u0_fifo(/*autoinst*/);
```

用 `AME`（emacs 风格，可直接配 EAP + 模板）：

```verilog
/* fifo  auto_template (
  ); */
fifo #(/*autoinstparam*/)   u0_fifo(/*autoinst*/);
```

### APM / AFM —— 参数 / 状态机骨架

`/*autopara*/ (A, B=2, C)` 展开为对齐的 `parameter` 声明：

```verilog
    /*autopara*/ (A, B=2, C)
// Define parameter here
parameter A = 2'd0;
parameter B = 2'd2;
parameter C = 2'd3;
// End of automatic parameter
```

`/*autofsm*/ (IDLE,RUN,DONE) state nstate` 展开为状态 localparam 加
双 always 块状态机骨架（状态寄存器 + 次态逻辑），状态位宽由状态数
推导。

### AH / ATpl —— 文件头与新文件

`AH` 前置文件头注释块（`// +FHDR`：版权、`Filename` / `Author` /
`Created On` / `Last Modified`、`Description`）。`ATpl foo.v` 按项目
骨架创建新文件（同样的 `+FHDR` 块之后）并打开——`_tb`/`tb` 后缀生成
testbench 骨架（时钟/复位生成器、FSDB 转储、`/*autodef*/` +
`/*autowire*/` + `/*autoreg*/` 标记、Local Variables 指向新文件自己
的目录）。新的空 `.v`/`.sv` 缓冲区自动套用骨架（BufNewFile）。

### BPN / BP / BA —— always 块片段

在光标处插入 always 骨架（光标留在第一个空分支里）：

```verilog
// BPN
always @(posedge clk or negedge rst_n) begin
    if(!rst_n) begin

    end else if() begin
    end else begin
    end
end

// BP:  always @(posedge clk) 带 if/else
// BA:  always @(*) begin end
```

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

## 模块文件如何查找

EAI/AIT/AW 需要子模块的源文件来读它的端口。搜索顺序：

1. 缓冲区 Local Variables 块里的 `verilog-library-directories` /
   `verilog-library-flags`（相对路径相对于**真实文件的目录**解析，
   `~` 与开头的 `$VAR` 会展开）
2. 其中列出的 `-f file.vc` 库文件（递归）
3. 正在编辑文件所在的目录，以及同一缓冲区内定义的模块
4. `g:verilog_tooling_libdirs` / 命令行 `-y` 目录

文件底部典型的 Local Variables 块：

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

## AUTO_TEMPLATE (EAI/EAP)

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

## AUTO_PYTHON 与 AUTO_LISP

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

多文件共享的辅助函数可以放在真正的 `.py` 文件里，用文件局部变量
`verilog-auto-python-file` 加载（在 `-y`/vc 库目录里搜，然后是缓冲区
自己的目录；定义按文件 mtime 缓存；文件局部的先加载，其次是行内的
`/*AUTO_PYTHON*/` 块——同名冲突时后定义者胜）。一切都在同一个无
builtins 沙箱下运行。

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
