# Run Eval

How to trigger evaluation execution, upload session data to Langfuse, and debug individual items.

---

## 执行模型

每个测试 item 作为独立的 **task subagent** 在 openclaw 中执行。每批并行 5 个，22 个 item 约 4-5 批。

```
openclaw agent（caw-eval skill）
  │
  ├── task: E2E-01L1  ─┐
  ├── task: E2E-01L2   ├── 并行执行（每批 5 个）
  ├── task: E2E-01L3   │
  ├── task: E2E-02L1   │
  └── task: E2E-02L2  ─┘
            │
            ▼（每批完成后）
  upload session × 5  →  Langfuse telemetry
```

---

## Prerequisites

```bash
# Session 上传必须的环境变量
export AGENT_WALLET_API_URL=https://api-agent-wallet-assistant.sandbox.cobo.com
export CAW_API_KEY=<your-caw-api-key>

# 验证 otel_report.py 可访问
ls <repo>/cobo-agent-wallet/assistant-backend/assistant/tests/e2e/opentelemetry/otel_report.py
```

> **不设置 `AGENT_WALLET_API_URL` / `CAW_API_KEY`：** session 不会被上传，`upload` 命令会报错。

---

## 获取 Item 列表

执行前，先获取数据集 item 列表，供 agent 分发给 task subagent：

```bash
cd <repo>/cobo-agent-wallet

# 文本格式（阅读用）
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py list \
  --dataset-name caw-agent-eval-v1

# JSON 格式（agent 解析用）
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py list \
  --dataset-name caw-agent-eval-v1 --format json

# 只看某个 item
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py list \
  --dataset-name caw-agent-eval-v1 --item-id E2E-01L1
```

---

## 执行 Task Subagent

对每个 item，agent 使用 `task` 工具创建独立执行上下文。建议每批 5 个并行。

Task prompt 模板：
```
你正在执行 CAW 评测 case {item_id}。
cobo-agentic-wallet-sandbox skill 已激活。
按照以下用户指令完成操作：

{user_message}

完成后，输出 session 文件路径以便上传。
```

---

## 上传 Session 文件

每批 task 完成后，对每个 item 执行 upload：

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py upload \
  --session /path/to/session.jsonl \
  --dataset-name caw-agent-eval-v1 \
  --item-id E2E-01L1 \
  --run-name eval-run-20260407-1000 \
  --api-url $AGENT_WALLET_API_URL \
  --api-key $CAW_API_KEY
```

**参数说明：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--session` | — | session.jsonl 文件路径（必须） |
| `--item-id` | — | 对应的 dataset item ID（必须） |
| `--run-name` | `eval-run-<timestamp>` | Langfuse run 名称（同一次评测保持一致） |
| `--api-url` | `$AGENT_WALLET_API_URL` | CAW backend telemetry endpoint |
| `--api-key` | `$CAW_API_KEY` | CAW API key |
| `--skill` | `cobo-agentic-wallet-sandbox` | Session 标签 |
| `--dataset-name` | `caw-agent-eval-v1` | Langfuse 数据集名称 |

---

## Session 文件位置

openclaw session 文件默认存储在：

- **Linux**：`~/.openclaw/agents/main/sessions/*.jsonl`
- **macOS**：`~/.claude/projects/<encoded-path>/*.jsonl`

task subagent 执行后，最新的 `.jsonl` 即为对应 session：
```bash
# 找到最近生成的 session 文件
ls -t ~/.openclaw/agents/main/sessions/*.jsonl | head -5
```

---

## How Session Upload Works

1. `run_eval.py upload` 从 session.jsonl 提取 `session_id`（第一个 `type=session` 事件）
2. 调用 `otel_report.py` 上传到 backend telemetry：
   ```bash
   python otel_report.py <session.jsonl> --trace-name cobo-agentic-wallet-sandbox
   ```
3. `session_id` 作为 `trace_id` 写入 Langfuse dataset item run

---

## Langfuse Run Linking

上传完成后，在 Langfuse UI 查看：
- **Datasets** → `caw-agent-eval-v1` → **Runs** tab
- 选择 run 名称，查看所有 item 的 trace 和输出

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `[PREFLIGHT ERROR] otel_report.py not found` | Script missing | Check `assistant-backend/` submodule or clone |
| `[UPLOAD ERROR] exit=1` | Missing API URL/key, or backend unreachable | Set `AGENT_WALLET_API_URL` and `CAW_API_KEY` |
| `[LINK ERROR] item not found` | Wrong dataset name or item ID | Verify with `list` subcommand |
| Session file not found after task | Task ran in different env | Check `~/.openclaw/agents/main/sessions/` |
