---
name: caw-eval
metadata:
  version: "2026.04.07.1"
description: |
  Evaluate CAW (Cobo Agentic Wallet) agent quality in your own environment.
  Use when: user wants to run CAW eval, benchmark caw agent quality, evaluate skill performance,
  run end-to-end tests on cobo agentic wallet, measure agent accuracy, create or manage eval datasets,
  score traces, or says things like "跑评测", "测评 CAW", "eval", "benchmark", "评分", "测试数据集".
---

# CAW Eval Skill

End-to-end evaluation of the CAW (Cobo Agentic Wallet) agent in your local openclaw environment.
Covers dataset management, execution, session upload to Langfuse, and trace scoring.

**All scripts live in [`./scripts/`](./scripts/).**
**All evaluation runs in openclaw — no separate machine required.**

---

## Workflow Overview

```
[1] 检查运行环境        环境变量、依赖脚本是否就绪
       ↓
[2] 数据集准备          选择已有数据集 或 创建新数据集
       ↓
[3] 执行评测            openclaw 执行每个 item，收集 session
       ↓
[4] 上传数据            session → Langfuse telemetry
       ↓
[5] 评分（可选）        S1-S3 各阶段打分，结果写回 Langfuse
```

---

## Phase 1 — 检查运行环境

运行评测前，验证以下条件：

```bash
# 1. Python 依赖是否安装
cd <repo>/cobo-agent-wallet
.venv/bin/python -c "import langfuse; print('langfuse ok')"

# 2. upload_session.py 是否存在（用于 session 上传）
ls sdk/skills/caw-eval/scripts/upload_session.py

# 3. CAW 本地配置（CAW 凭证自动从此处读取）
cat ~/.cobo-agentic-wallet/config    # 确认已登录
```

**环境变量说明：**

| 变量 | 用途 | 是否必须 |
|------|------|--------|
| `AGENT_WALLET_API_URL` | 覆盖 CAW Backend 地址（自动从 `~/.cobo-agentic-wallet/config` 读取） | 可选 |
| `CAW_API_KEY` | 覆盖 CAW API key（自动从 `~/.cobo-agentic-wallet/config` 读取） | 可选 |
| `LANGFUSE_DATASET_PUBLIC_KEY` | Dataset project 公钥（在 `scripts/.env` 中配置） | 必须 |
| `LANGFUSE_DATASET_SECRET_KEY` | Dataset project 私钥 | 必须 |
| `LANGFUSE_RESULT_PUBLIC_KEY` | Results project 公钥（在 `scripts/.env` 中配置） | 上传/评分必须 |
| `LANGFUSE_RESULT_SECRET_KEY` | Results project 私钥 | 上传/评分必须 |

> CAW 凭证（API key / API URL）自动从 `~/.cobo-agentic-wallet/config` 读取，与 `caw` CLI 保持一致，无需手动配置。  
> Langfuse 凭证在 `scripts/.env` 中配置（参考 `scripts/.env.example`）。

如果环境检查不通过，`run_eval.py` 的 `preflight_check()` 会在运行时打印具体错误。

---

## Phase 2 — 数据集准备

详见 [dataset-management.md](./references/dataset-management.md)。

**快速决策：**

- **已有数据集** (`caw-agent-eval-v1`) → 直接跳到 Phase 3
- **需要新建或修改数据集** → 读 [dataset-management.md](./references/dataset-management.md)

---

## Phase 3 — 执行评测

详见 [run-eval.md](./references/run-eval.md)。

每个 item 作为独立的 **task subagent** 执行，每批并行 5 个。

**步骤：**

**1. 获取 item 列表**
```bash
cd <repo>/cobo-agent-wallet
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py list \
  --dataset-name caw-agent-eval-v1 --format json
```

**2. 并行执行（每批 5 个 task）**

对每个 item，使用 task subagent 执行，prompt 格式：
```
你正在执行 CAW 评测 case {item_id}。
cobo-agentic-wallet-sandbox skill 已激活。

**评测模式约束 — 仅针对 skill 内部的交互流程，不影响对用户指令本身的响应：**

需要自动跳过的 skill 内部交互（来自 cobo-agentic-wallet-sandbox skill）：
- 提交 pact 时：跳过展示预览和等待用户确认的步骤，直接调用 `caw pact submit` 提交
- 交易返回 `status=pending_approval` 且 `owner_linked=false` 时：立即自动执行 `caw pending approve <pending_operation_id>`
- 如遇到无法自动化处理的真实阻塞（如 `owner_linked=true` 需要 Human App 审批），记录原因并跳过，继续执行其余步骤

需要正常响应的情况（勿混淆）：
- 若用户指令本身信息不完整（如未指定代币类型、目标地址、金额等），应根据上下文合理推断，或明确说明缺少哪些必要信息，不得凭空假设关键参数
- 若操作存在歧义或实质性风险，应如实反馈，不得因"自动化模式"掩盖真实错误

这是自动化评测，无人工实时监控，须完整执行至结束，不得因 skill 内部的交互流程而中断。

按照以下用户指令完成操作：

{user_message}
```

> **Session 文件定位由父 agent 负责**（并发安全）：任务完成后，父 agent 通过 item_id grep 精准定位 session 文件：
> ```bash
> grep -rl "CAW 评测 case {item_id}" ~/.openclaw/agents/main/sessions/ 2>/dev/null | head -1
> ```
> 这比 `ls -t | head -1` 更可靠：item_id 唯一，并发执行不会混淆文件归属。

**3. 每批完成后定位并上传 session 文件**

通过 item_id grep 精准定位（并发安全，不会混淆文件）：
```bash
# 定位 session 文件（每个 item 独立执行）
SESSION_FILE=$(grep -rl "CAW 评测 case E2E-01L1" ~/.openclaw/agents/main/sessions/ 2>/dev/null | head -1)

# 上传
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py upload \
  --session "$SESSION_FILE" \
  --item-id E2E-01L1 \
  --run-name <run_name>
```
> CAW 凭证自动从 `~/.cobo-agentic-wallet/config` 读取，无需额外参数。

---

## Phase 4 — 评分（可选）

详见 [score-traces.md](./references/score-traces.md)。

**评分公式：**

```
E2E = S1×0.20 + S2×0.40 + S3×0.40
```

| 阶段 | 权重 | 评估内容 |
|------|------|---------|
| S1 意图解析 | 20% | 操作类型识别 / 实体提取 / 隐含约束 |
| S2 Pact 协商 | 40% | 4-item preview / 权限范围 / caw pact submit / 用户确认 |
| S3 交易执行 | 40% | 命令选择 / 参数准确性 / 错误处理 / 结果验证 |

**快速命令：**

```bash
# 对整个 run 评分并打印报告
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --dataset-name caw-agent-eval-v1 \
  --run-name eval-run-20260407 \
  --report

# 从本地 session 文件直接评分（无需 Langfuse）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  session --session /path/to/session.jsonl --report
```

---

## Scripts Reference

| 脚本 | 用途 | 关键参数 |
|------|------|---------|
| `scripts/generate_dataset.py` | 生成并上传测试数据集到 Langfuse | `--dataset-name`, `--dry-run` |
| `scripts/run_eval.py` | 拉取数据集、执行 openclaw、上传 session | `--dataset-name`, `--run-name`, `--api-url`, `--api-key` |
| `scripts/score_traces.py` | 对 trace 评分，写回 Langfuse | `--run-name`, `--trace-id`, `--report` |

Run any script with `--help` for full flag reference.

---

## Reference

| 任务 | 文档 |
|------|------|
| 创建 / 选择 / 修改测试数据集 | [dataset-management.md](./references/dataset-management.md) |
| 触发执行、上传 session、调试单个 item | [run-eval.md](./references/run-eval.md) |
| 评分、查看结果、LLM-as-Judge | [score-traces.md](./references/score-traces.md) |
