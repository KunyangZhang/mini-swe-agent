# 明早验收

## 已有本地环境（WSL）

全部内容位于 `E:\Codex\简历\Coding-Agent-Harness`。在 WSL 终端执行：

```bash
cd '/mnt/e/Codex/简历/Coding-Agent-Harness/mini-swe-agent'
HARNESS_PYTHON='/mnt/e/Codex/简历/Coding-Agent-Harness/.venv-harness/bin/python' \
  bash scripts/accept_durable.sh --full
```

命令必须退出 0，并打印 `Acceptance passed` 和新产物目录。默认产物位于项目旁的 `acceptance/run-*`，不会覆盖已提交的实验。约数分钟；不用联网、不用 GPU、不用凭据。它会：

1. 从 Git 取原提交的 src，用当前的三条边界测试复现 **恰好三个失败**，并确认不是测试加载错误。
2. 在修复版本执行 **104 项相关测试**。
3. 复现原七位置实验。
4. 重做六个编码案例：真实源码修改、8 项测试、执行次数、核验恢复、重复副作用对照，以及预期失败的工作区漂移负例。负例被正确识别为失败时，实验验收通过。
5. 使用 `--full` 时复现原 25 次重复实验；这些重复不算新增场景。

只想快速检查修复：

```bash
'/mnt/e/Codex/简历/Coding-Agent-Harness/.venv-harness/bin/python' \
  -m pytest tests/harness/test_boundaries.py -q
```

预期 `6 passed`。从两个不同进程恢复同一批次时，只有持锁进程可以执行；锁争抢失败不是任务失败率样本。

## 在新的 Linux/WSL 环境

```bash
git clone --branch fix/durable-recovery-evidence-20261017 \
  https://github.com/KunyangZhang/mini-swe-agent.git
cd mini-swe-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r evidence/20261017/requirements-local.txt
.venv/bin/python -m pip install --no-deps -e .
HARNESS_PYTHON="$PWD/.venv/bin/python" bash scripts/accept_durable.sh --full
```

本次本地 Python 为 3.12.3；依赖快照是已测组合，并非跨任意未来平台的兼容保证。若系统没有 venv，先安装发行版的 Python venv 支持，或使用 uv 创建环境。`PYTEST_ADDOPTS=--capture=sys` 是验收脚本为 WSL DrvFS 临时目录设置的兼容选项，不改变测试断言。

## 证据入口

- `docs/experiments/durable-20261017-zh.md`：问题、改动、条件、结果、限制。
- `docs/experiments/durable-resume-interview-zh.md`：基于证据的简历和面试稿。
- `evidence/20261017/`：原测试复现、失败复现、回归、编码逐案例、真实模型轨迹、环境与版本。
- 原有结果仍在 `evidence/recovery-report.json`、`evidence/repeated-recovery.json` 等文件，本次未覆盖它们。

编码证据中的源码副本使用 `.py.txt`，避免被全仓 pytest 误收集；压缩包保留实际工作区、SQLite 日志与原文件名。直接复现请使用验收命令创建新工作区，不要在证据快照目录继续运行旧会话。

## 真实模型（单独验收，有 GPU 才执行）

AutoDL 原机器：`ssh -p 49137 root@connect.westb.seetacloud.com`。密码不写入仓库。登录后：

```bash
cd /root/autodl-tmp/Coding-Agent-Harness/mini-swe-agent
export PATH=/root/miniconda3/bin:$PATH
python -m tests.harness.live_campaign \
  --model-path /root/autodl-tmp/Coding-Agent-Harness/model \
  --output /root/autodl-tmp/Coding-Agent-Harness/live-recheck-$(date +%Y%m%d-%H%M%S)
```

源码直接作为初始上下文的独立条件，在同一命令追加 `--include-source`。两组提示条件必须分别报告，不能将后者掩盖前者的失败。模型集成脚本记录所有结果，无论成功与否都可能正常退出；请以 report.json 中的 `task_success`、测试哈希和 acceptance 输出验收，不能仅看进程退出码。

模型依赖版本和文件 SHA256 见 remote 环境证据。下载方法（在此机器已有文件，无需重下）：

```bash
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download('Qwen/Qwen2.5-Coder-3B-Instruct',
                  local_dir='/root/autodl-tmp/Coding-Agent-Harness/model', max_workers=4)
PY
```

独立的 local-transformers 适配器只用于此实验，不声称已经验证上游所有线上模型 adapter。生成使用 greedy；不同 GPU、库版本或内核仍可能导致不同轨迹。
