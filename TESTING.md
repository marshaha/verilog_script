# verilog_tooling 测试与门禁(对面 agent 必读)

## 环境
- Python 3.10+(开发用 3.13),`pip install pytest`(或建 .venv)
- 跑 vm-repo 官方 golden 对照需要 emacs:`brew install emacs` / `apt install emacs`

## 目录约定
- `tests/` pytest 套件;`src/verilog_tooling/` 被测包
- `verilog-mode-repo/` 放任意位置,用环境变量 `VM_REPO_DIR` 指过去
  (默认 /tmp/verilog-mode-repo);不存在时 test_vm_repo.py 自动 skip
- `verilog-mode.el`(仓库根就有)给 `tests/verify_against_emacs.sh` 用,
  默认读 /tmp/verilog-mode.el,不存在时脚本会重新 curl 下载

## 门禁(改代码后按序跑,全绿才算完)
1. `pytest -q` —— 当前基线 601 passed, 4 skipped, 75 xfailed(0 failed)
   - xfail 是跟踪中的已知差异;修好后出现 XPASS 就把文件名从
     tests/test_vm_repo.py 的 KNOWN_FAILURES 列表删掉锁定
2. `bash tests/verify_against_emacs.sh` —— 必须 7 场景全 IDENTICAL
   (我们的 EAI/EAP 等与真 emacs 逐字节对照)
3. 有 RTL 工程的话:`python3 closed_loop.py <rtl_dir> --aall`
   (幂等性 + verilator lint 零新增错误)

## 铁律
- 行为分歧时用真 emacs 做实验推导规则,不要猜(golden 可能过时,
  harness 会用当前 emacs 基线兜底)
- 新增命令要同步:CLI 子命令、vim 插件映射、README 中英文、测试
