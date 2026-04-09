# Score Traces

How to score CAW agent evaluation traces using the 3-stage rubric, and how to read/interpret results.

---

## Scoring Architecture

每条 trace 按 **3 个执行阶段** 独立打分（0-10 分），加权得出 E2E 综合分：

```
E2E = S1×0.20 + S2×0.40 + S3×0.40
```

| 阶段 | 权重 | 评估内容 |
|------|------|---------|
| S1 意图解析 | 20% | 操作类型识别 / 实体提取 / 隐含约束识别 |
| S2 Pact 协商 | 40% | Pact 提案质量 / 权限范围正确性 / caw pact submit / 用户确认 |
| S3 交易执行 | 40% | 命令选择 / 参数准确性 / denial/error 处理 / 结果验证 |

S2 和 S3 各占 40%，对等权重。结果报告作为 S3 的 `result_verification` 维度内嵌评估，不单独成阶段。

**主评分**：`score_traces.py` 通过 openclaw 子 agent（`claude --print`）直接评估所有 S1-S3 维度及综合指标，返回结构化 JSON 分数。评分结果带有来源标记 `caw.scoring_source`（1.0=子 agent / 0.0=启发式）。
**备用评分**：找不到 openclaw 时自动退回**启发式规则**（关键词匹配 + 结构检查），确保流程不中断。

---

## Usage

### 对整个 Run 评分

```bash
cd <repo>/cobo-agent-wallet

.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --dataset-name caw-agent-eval-v1 \
  --run-name eval-run-20260407 \
  --report
```

**输出：** 打印每个 trace 的 S1-S3 明细 + E2E 综合分，结果写回 Langfuse Scores。

### 对单个 Trace 评分（调试）

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --trace-id <trace_id>
```

### 从本地 Session 文件评分

不依赖已上传的 trace，直接从 `.jsonl` 解析会话内容。建议通过 `--item-id` 提供 dataset item 上下文，以获得准确的评分：

```bash
# 带 item 上下文（推荐）：从 Langfuse dataset project 拉取对应 item 的 expected output + metadata
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  session \
  --session /path/to/session.jsonl \
  --item-id E2E-01L1 \
  --dataset-name caw-agent-eval-v1 \
  --report

# 不带 item 上下文（通用启发式，精度较低）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  session --session /path/to/session.jsonl --report

# 目录下所有 session（批量，无 item 上下文）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  session --session ~/.claude/projects/<encoded-path>/ --report

# 只评分不上传 Langfuse
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  session --session /path/to/session.jsonl --report --dry-run
```

### 读取已有评分

通过 Langfuse SDK 或 UI 查看评分（无需脚本）：

```python
from langfuse import Langfuse
lf = Langfuse()
trace = lf.get_trace("<trace_id>")
for score in trace.scores:
    print(f"{score.name}: {score.value}")
```

### 保存结果到文件

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --run-name eval-run-20260407 \
  --output /tmp/eval-results.json \
  --report
```

---

## Key Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dataset-name` | `caw-agent-eval-v1` | Langfuse dataset 名称 |
| `--run-name` | — | 对整个 run 评分时必须 |
| `--trace-id` | — | 对单个 trace 评分 |
| `--report` | — | 打印详细分数报告 |
| `--dry-run` | — | 计算但不写回 Langfuse |
| `--output PATH` | — | 结果写入 JSON 文件 |
| `subcommand: session` | — | 从本地 .jsonl 评分 |
| `session --item-id` | — | Dataset item ID，用于从 dataset project 加载 expected output + metadata |
| `session --dataset-name` | `caw-agent-eval-v1` | 与 `--item-id` 配合，指定数据集名称 |

---

## How Scores Are Uploaded

每条 trace 评分完成后，`score_traces.py` 通过 **Langfuse SDK 直接写入**（无需 CAW 后端）：

1. **创建新的 evaluator Trace**（Langfuse）：
   - type: `evaluator`
   - output: 各阶段详细评分 JSON
   - 与原始 trace 通过 `session_id`/`trace_id` 关联

2. **写入 Langfuse Scores** 到原始 trace：
   ```
   caw.s1_intent       → S1 意图解析阶段分
   caw.s2_pact         → S2 Pact 协商阶段分
   caw.s3_execution    → S3 交易执行阶段分（含结果验证）
   caw.e2e_composite   → 综合加权分
   caw.scoring_source  → 1.0=subagent / 0.0=heuristic
   ```

3. 在 Langfuse UI 中可通过 **Scores** 面板查看各维度趋势。

---

## Scoring Dimensions Detail

### S1 — 意图解析 (20%)

| 维度 | 权重 | 评分逻辑 |
|------|------|---------|
| operation_type_accuracy | 40% | 是否正确识别 transfer/swap/lend/bridge/dca/query |
| entity_extraction | 40% | token/amount/chain/address 是否从 user_message 中提取 |
| constraint_recognition | 20% | 隐含约束（滑点/周期/gas 预留）是否被识别 |

### S2 — Pact 协商 (40%)

CAW skill 要求在每次提交 pact 前展示 4-item preview（Intent / Execution Plan / Policies / Completion Conditions）并获得用户确认。

| 维度 | 权重 | 评分逻辑 |
|------|------|---------|
| pact_proposal_quality | 30% | 是否展示了 4-item preview（Intent/Plan/Policies/Conditions） |
| scope_correctness | 30% | 权限类型 / chain / amount / token 限制是否正确 |
| pact_submitted | 25% | `caw pact submit` 是否实际被调用 |
| confirmation_obtained | 15% | 是否在提交前获得了用户确认 |

### S3 — 交易执行 (40%)

| 维度 | 权重 | 评分逻辑 |
|------|------|---------|
| command_selection | 30% | 是否选择了正确的 caw 命令（transfer/call/dca/query） |
| parameter_accuracy | 35% | --to/--amount/--token-id/--chain-id/--pact 参数是否正确 |
| error_handling | 20% | 遇到 denial/policy/insufficient 时是否按规范处理 |
| result_verification | 15% | 是否报告了 tx ID/status/amount；pending_approval 正确处理 |

---

## Subagent Judge（两阶段协议）

LLM-as-a-Judge 通过 **Copilot task subagent** 执行，脚本本身不直接调用任何 LLM API。

### 阶段一：生成 judge prompt 文件

```bash
# 从 Langfuse run 生成 judge 请求文件
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --run-name eval-run-20260407 \
  --dump-judge-requests /tmp/judge_requests.json

# 从本地 session 文件生成 judge 请求文件
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session /path/to/sessions_dir/ \
  --item-id E2E-01L1 \
  --dump-judge-requests /tmp/judge_requests.json
```

脚本生成一个 JSON 数组，每条记录包含 `{trace_id, item_id, metadata, system_prompt, prompt, session_path}`。

### 阶段二：Copilot 执行 task subagent

Copilot（openclaw）读取 `judge_requests.json`，对每条请求使用 `task` tool 启动 subagent，将评分结果写入 `judge_results.json`。

结果格式为 JSON 数组，每条记录包含：
```json
{
  "trace_id": "...",
  "available": true,
  "s1": 0.8, "s2": 0.7, "s3": 0.9,
  "task_completion": 8,
  "recipe_hit_quality": 7,
  "pact_intent_match": 9,
  "error_type": "none",
  "recovery_logic": 8,
  "hallucination_risk": 1,
  "e2e": 0.8,
  "comment": "..."
}
```

### 阶段三：应用评分结果

```bash
# 从 Langfuse run 应用评分
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py \
  --run-name eval-run-20260407 \
  --judge-results /tmp/judge_results.json \
  --report

# 从本地 session 文件应用评分
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session /path/to/sessions_dir/ \
  --judge-results /tmp/judge_results.json
```

> 未提供 `--judge-results` 时，自动退回到纯启发式评分，不影响流程。

---

## Reading Results in Langfuse

评分写回后，在 Langfuse UI 查看：

1. **Datasets** → `caw-agent-eval-v1` → **Runs** tab → 选择 run
2. 每个 item 右侧可见 `caw.e2e_composite` 综合分
3. 点击单个 trace → **Scores** 面板 → 查看 S1-S3 明细

也可通过 SDK 读取：
```python
from langfuse import Langfuse
lf = Langfuse()
trace = lf.get_trace("<trace_id>")
for score in trace.scores:
    print(f"{score.name}: {score.value}")
```

---

## Troubleshooting

| 症状 | 原因 | 解决 |
|------|------|------|
| `No items found for run X` | run_name 拼写错误或 run 未关联 | 确认 `run_eval.py` 的 `--run-name` 与此处一致 |
| 所有 S2 分数偏低 | Pact 命令未被调用或 session 无 pact 相关内容 | 检查 `caw pact submit` 是否出现在 session |
| Subagent judge 未生效 | `claude` CLI 不在 PATH 中 | 确认 `which claude` 可找到 openclaw 二进制 |
| 分数未在 Langfuse 出现 | Langfuse 凭证错误 | 验证 `LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY` |
| `--dry-run` 后无 Scores | 预期行为，dry-run 不写回 | 去掉 `--dry-run` 后重新运行 |


