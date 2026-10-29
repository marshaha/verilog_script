# 贡献规矩（Contributing）

两条规矩，立于 2026-10-29。违反其中任何一条的改动不合入。

## 规矩 1：共享管线状态只进 AallContext，不再逐个传参

- 一次 aall 运行内共享的状态（目前是 per-span 实例 pin 扫描）只放在
  `python/verilog_tooling/wire.py` 的 `AallContext` 里，以字段形式存在。
- 新的共享缓存 = 给 `AallContext` 加一个字段，并在 `_main_aall` 里初始化。
  不许再给 pass 函数加一个缓存参数。`pin_scan_cache` 逐个传参的时代
  已经结束（它最后一次出现是在 21 个函数签名里，见 2026-10-29 的迁移）。
- Pass 函数的签名只收 `ctx: AallContext | None`；函数体内解包一次
  （`pin_scan_cache = ctx.pin_scan_cache if ctx is not None else None`），
  再往下传原始 dict。模块内的 `_single` 助手保持收原始 dict，不用改。
- `ctx=None` 永远表示“没有共享状态，各 pass 内联扫描”，行为必须与
  有 ctx 时逐字节一致（测试会钉住这一点）。

为什么：2026-10-28 的 5fb8dd7 直接把 `pin_scan_cache` 传进 auto_arg，
却没注意到 cache 是 per-span 而非整 buffer，多模块 buffer 丢了后几个
模块的 inout 引脚，trunk 以 1c08c7a 修回。参数能传到，语义不一定跟上；
收进一个对象，语义只有一个地方需要讲对。

## 规矩 2：quirk 与分歧修复，先有复现测试，再改代码

- 每个 Emacs 分歧 / quirk / 边界行为修复，必须先写一个在旧代码上
  失败的测试（复现），再改代码让它变绿。测试与修复同批提交。
- 覆盖面：单模块过了不算完——多模块 buffer、.v/.sv 两种方言、带参实例
  都要想到。5fb8dd7 的教训：单模块测试 + 单模块 bench 盖不住多模块。
- 行为分歧以真 Emacs 为准，不要猜（TESTING.md 铁律同样要求这一点）。
- 门禁（改代码后按序跑，全绿才算完，细则见 TESTING.md）：
  1. `pytest -q`（当前基线 964 passed / 2 skipped / 27 xfailed，0 failed）
  2. `bash tests/verify_against_emacs.sh`（7 场景全 IDENTICAL）
  3. 对比脚本 cmp2：265 PARITY / 24 DIFF（名单不变）
  4. bench 输出与基线逐字节一致
- CI（`.github/workflows/test.yml`）在每次 push 与 PR 上跑 pytest；
  红了先修门禁，再谈合入。分支保护未开，CI 目前只报告、不拦截——
  但红树绝不能 push（此条此前已立，此处重申）。

## 其他已成文的约定

- Python 3.7 floor：`BooleanOptionalAction` 不可用，三态用
  store_true/store_false 双 action 同 dest；无 possessive 量词与原子组。
- 正则必须容忍空格变化（`)`、`;`、`]` 周围 `\s*`）。
- 修复走真实 CLI（`aall`/`ainj` 入口）验证，不只测内部 API；纯重构
  无行为变化时用守卫测试（旧码挂、新码过）代替。
- 报性能比值前须独立核验 Emacs 退出码与展开数；含 error traceback
  的 Emacs 输出不算完整展开。
