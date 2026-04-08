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
# 验证 upload_session.py 可访问
ls <repo>/cobo-agent-wallet/sdk/skills/caw-eval/scripts/upload_session.py

# 验证 CAW 本地配置已登录（凭证自动读取，无需手动 export）
cat ~/.cobo-agentic-wallet/config
```

> **CAW 凭证（API URL / API Key）** 自动从 `~/.cobo-agentic-wallet/config` 读取，与 `caw` CLI 保持一致，无需手动设置环境变量。  
> 如需覆盖，可设置 `AGENT_WALLET_API_URL` 和 `CAW_API_KEY` 环境变量。

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

完成后，用 bash 找到并输出本次 session 文件的完整路径（注意是 .jsonl 文件，不是 sessions.json）：
ls -t ~/.openclaw/agents/main/sessions/*.jsonl 2>/dev/null | head -1
```

---

## 上传 Session 文件

每批 task 完成后，对每个 item 执行 upload：

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval.py upload \
  --session /path/to/session.jsonl \
  --dataset-name caw-agent-eval-v1 \
  --item-id E2E-01L1 \
  --run-name eval-run-20260407-1000
```

**参数说明：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--session` | — | session.jsonl 文件路径（必须） |
| `--item-id` | — | 对应的 dataset item ID（必须） |
| `--run-name` | `eval-run-<timestamp>` | Langfuse run 名称（同一次评测保持一致） |
| `--api-url` | 自动从 `~/.cobo-agentic-wallet/config` 读取 | 覆盖 CAW backend telemetry endpoint（可选） |
| `--api-key` | 自动从 `~/.cobo-agentic-wallet/config` 读取 | 覆盖 CAW API key（可选） |
| `--skill` | `cobo-agentic-wallet-sandbox` | Session 标签 |
| `--dataset-name` | `caw-agent-eval-v1` | Langfuse 数据集名称 |

---

## Session 文件位置

openclaw session 文件默认存储在：

- **Linux / macOS**：`~/.openclaw/agents/main/sessions/*.jsonl`

> 注意：同目录下的 `sessions.json`（无 `.jsonl` 后缀）是元数据文件，不是 session 数据。使用 `*.jsonl` 通配符可自动排除。

task subagent 执行后，最新的 `.jsonl` 即为对应 session：
```bash
# 找到最近生成的 session 文件（最新的就是刚完成的 task 的 session）
ls -t ~/.openclaw/agents/main/sessions/*.jsonl 2>/dev/null | head -5
```

---

## How Session Upload Works

1. `run_eval.py upload` 从 session.jsonl 提取 `session_id`（第一个 `type=session` 事件）
2. 调用 `upload_session.py` 上传到 backend telemetry，CAW 凭证自动从 `~/.cobo-agentic-wallet/config` 读取：
   ```bash
   python upload_session.py <session.jsonl> --trace-name cobo-agentic-wallet-sandbox
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
| `[PREFLIGHT ERROR] upload_session.py not found` | Script missing | 确认在正确的 repo 路径下执行 |
| `[UPLOAD ERROR] exit=1` | CAW 未登录或 backend 不可访问 | 检查 `~/.cobo-agentic-wallet/config`，或设置 `AGENT_WALLET_API_URL` 和 `CAW_API_KEY` |
| `[LINK ERROR] item not found` | Wrong dataset name or item ID | Verify with `list` subcommand |
| Session file not found after task | Task ran in different env | Check `~/.openclaw/agents/main/sessions/*.jsonl` |
