# Claude Code 评测：执行步骤

**本文件是标准模式 + CC headless 评测的 Agent 执行指南。** 从本地 Mac 并行 dispatch 到服务器跑 `claude -p`，本地评分出报告。

公共环境配置（gcloud / SSH / 服务器同步 / troubleshooting）见 [common-execution.md](./common-execution.md)。

> **正式评测必须跑在服务器**（`dispatch --server`）。本地 `claude -p` 降级为仅供开发调试——
> 实测同 pilot 本地 vs 服务器 E2E 差 0.18（本地 skill/caw/context 漂移）。
> 守卫：`runtime_compliance.py --check-session-source <RUN_DIR> --strict`（发布前必跑）。

---

## Step 1: 环境识别 + gcloud 配置

参见 [common-execution.md](./common-execution.md)。

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11   # gcloud ≥ 565 要求 Python 3.10+
```

## Step 2: 选目标服务器 + 同步 skill/scripts/caw-cli

CC 评测按模式使用专用服务器池（与 openclaw 弱模型池区分，避免互相干扰）。格式 `name:zone:project`：

```bash
# ── CC + Recipe（recipe_mode=cc_with_recipe）：3 台（04-23 新建） ────────
SERVERS_CC_RECIPE=(
  "luochong-openclew-dev-v1-20260423-080555-test09:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260423-080800-test10:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260423-080854-test11:asia-east2-c:openclaw-keq9xwm4"
)

# ── CC 无 Recipe / 标准 CC 评测（recipe_mode=cc_no_recipe 或不带 recipe 模式）：3 台（04-23 新建） ──
SERVERS_CC_NO_RECIPE=(
  "luochong-openclew-dev-v1-20260423-080943-test12:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260423-081051-test13:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260423-081206-test14:asia-east2-c:openclaw-keq9xwm4"
)


# 按模式选择
SERVERS=("${SERVERS_CC_RECIPE[@]}")     # CC + recipe
# SERVERS=("${SERVERS_CC_NO_RECIPE[@]}") # CC 无 recipe / 标准 CC 评测
```

**重要**：
- **Recipe 对比评测必须两组同时跑**：`cc_with_recipe` 走 `SERVERS_CC_RECIPE`、`cc_no_recipe` 走 `SERVERS_CC_NO_RECIPE`，两个 dispatch 可并行，结果分别落在各自 run_name。
- **标准 CC 评测**（非 recipe）默认走 `SERVERS_CC_NO_RECIPE`。
- 不要跨池混用——两组服务器状态（caw 二进制版本、余额、历史 session）可能不同。
- openclaw 弱模型池见 [run-eval-openclaw.md Step 1](./run-eval-openclaw.md)，与本池独立。

**先 `git pull` 拉最新 master**（见 [common-execution.md "前置：拉取最新 master"](./common-execution.md)），再执行 sync。**不要 stash 本地未提交修改**；若 `git pull` 有冲突，停下来让用户手动解。

```bash
export SERVERS_ENV="${SERVERS[*]}"   # 空格分隔

bash sdk/skills/caw-eval/scripts/sync_to_servers.sh --component all --verify --servers-env SERVERS_ENV
```

---

## Step 3: Dispatch

```bash
cd <repo>/cobo-agent-wallet

DATASET_NAME=standard-test-v3   # 标准模式默认；recipe 模式改用 recipe-test-v3
RUN_NAME=eval-cc-sonnet-$(date +%Y%m%d-%H%M)

.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model sonnet \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)
```

`dispatch` 子命令：
1. **Busy check**：并行探 N 台，跳过有 `claude -p` / `openclaw agent eval-` 在跑的机器
2. **Precheck**（R2）：对比本地 vs 服务器各组件 hash（skill / scripts / caw-binary）；不一致 abort
3. **Deployment snapshot**（R3）：采集 git hash / content hash，写 `deployment_snapshot.json`
4. **动态队列**：N 台 worker 各自取 item，远端 `claude -p` headless 执行
5. 每个 item 执行完 scp 拉 session 回本地 `~/.caw-eval/runs/$RUN_NAME/<item_id>.jsonl`
6. **Recipe archive postcheck**（recipe 模式）：读服务器 archive hash 对比本地 manifest

### 选项

| flag | 含义 |
|------|------|
| `--timeout 900` | 单 item 超时（默认 600） |
| `--item-id E2E-01L1 E2E-06L1` | 只跑部分 item |
| `--no-sync-scripts` | 跳过 scripts 同步（假设服务器已同步） |
| `--force` | 忽略 busy / precheck 失败强制跑（不推荐） |
| `--no-precheck` | 跳过 precheck（正式评测禁用） |
| `--eval-mode recipe --recipe-mode cc_with_recipe` | Recipe 模式（见 [run-eval-recipe.md](./run-eval-recipe.md)） |

---

## Step 4: 提取运行指标

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py metrics \
  --run-name "$RUN_NAME"
```

从 session 文件提取：时长（秒）/ output tokens / 工具调用数 / caw 命令数 / pact submit 次数 / tx 命令数 / 错误数。写入 `~/.caw-eval/runs/$RUN_NAME/session_metrics.json`，供报告 Section 3 使用。

---

## Step 5: 上传 session 到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py upload \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME"
```

脚本为每个 session 生成独立 Langfuse trace（UUID），关联到 dataset run；同时写 `trace_map.json`（item_id → trace UUID）供评分使用。

确认输出每个 item 都显示 `[LINKED]`。

---

## Step 6: 生成精细版 judge prompt

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/$RUN_NAME/ \
  --dataset-name "$DATASET_NAME" \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
```

生成 judge request（断言结果 + pact 参数 + expected output）。

---

## Step 7: LLM Judge 评分（Sonnet subagent 并行）

读 `judge_req.json`，每个 request 启一个后台 Sonnet subagent。**保持 4-5 个并行**，一个完成补一个。每个 subagent 通过 Read 工具读完整 session 文件后评分，结果写 `judge_{item_id}.json`。

```python
run_dir = "~/.caw-eval/runs/{run_name}"

Agent(
    model="sonnet",
    run_in_background=True,
    description="Judge {item_id}",
    prompt="""你是 CAW Agent 评估专家。请对以下 session 进行评分。

{prompt}  # judge_req.json 中该 item 的 prompt 字段（含 session_path 和评分维度）

将结果写入 {run_dir}/judge_{item_id}.json，格式：
{{
  "item_id": "{item_id}",
  "intent_understanding": {{"score": 0.0, "reasoning": "..."}},
  "policies_correctness": {{"score": 0.0, "reasoning": "..."}},
  "completion_conditions_correctness": {{"score": 0.0, "reasoning": "..."}},
  "execution_correctness": {{"score": 0.0, "reasoning": "..."}},
  "result_reporting": {{"score": 0.0, "reasoning": "..."}},
  "task_completion": {{"score": 0.0, "reasoning": "..."}}
}}

should_refuse case 只需输出 refusal_quality 和 task_completion 两个维度。"""
)
```

所有 judge 完成后合并：

```python
import json, glob
run_dir = "~/.caw-eval/runs/{run_name}"
results = []
for f in sorted(glob.glob(f"{run_dir}/judge_E2E-*.json")):
    results.append(json.loads(open(f).read()))
open(f"{run_dir}/judge_results.json", "w").write(json.dumps(results, indent=2, ensure_ascii=False))
```

---

## Step 8: 应用评分到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/$RUN_NAME/ \
  --dataset-name "$DATASET_NAME" \
  --judge-results ~/.caw-eval/runs/$RUN_NAME/judge_results.json \
  --report
```

评分写入 Step 5 上传的各 trace（通过 `trace_map.json` 定位），Langfuse dataset run 页面可按维度查看。

---

## Step 9: 生成报告

主会话已有全部评测上下文，直接在主会话里写报告。

### 9.1 步骤
1. Read `~/.caw-eval/runs/{run_name}/judge_results.json` 按 e2e_composite 排序
2. Read `~/.caw-eval/runs/{run_name}/session_metrics.json` 填"运行指标"章节（CC 模式有）
3. **写 P0/P1 finding 之前**，先 grep 对应 case 的 session 原文拿 error excerpt——最便宜的证据来源：
   ```bash
   grep -B1 -A5 -iE "error|validation|failed|denied|exception|revert" \
     ~/.caw-eval/runs/{run_name}/E2E-<ITEM>.jsonl | head -40
   ```
4. 低分 case（e2e_composite < 0.6）Read 对应 `~/.caw-eval/runs/{run_name}/E2E-*.jsonl` 追根因
5. 疑似**产品源码**（后端 / CLI）问题时 Read `cobo-agent-wallet/src/app/` 或 `cobo-agent-wallet/sdk/go/` 对应文件验证（归 🔴 产品代码）
6. 疑似 SKILL 指令缺陷时 Read `cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-sandbox-dev/` 对应文件验证
   - CC 模式 skill 路径是 `cobo-agentic-wallet-sandbox-dev`（非 openclaw 的 `-sandbox`）
7. 输出到 `cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-{run_name}.md`

### 9.2 分析要求
- **深度分析硬性要求（所有 finding 适用）**：见 [issue-attribution.md "深度分析硬性要求"](./issue-attribution.md#深度分析硬性要求所有-finding-适用不分层)
  - 四段式 **现象 → 证据（含 file:line/字段/log 锚点）→ 根因 → Action Item**
  - **每条 finding 必须严格按 [issue-attribution.md 强制 Markdown 模板](./issue-attribution.md#强制-markdown-模板每条-finding-必用)**（证据是独立一行，不能塞进根因段里混合叙事）
  - 证据必须本会话真实 Read 过；找不到证据的 finding 降级为 `🔍 疑似` 且首条 Action 是"验证步骤"（见 [降级版模板](./issue-attribution.md#强制-markdown-模板每条-finding-必用)）
  - 🔵/🟢/🟡/🟤/🟠/🔴/🟣 任意层的根因都要读相应证据，不要因层次分布"看起来不对"就跳层归因
- **问题归因（必做）**：每条 finding 按 7 层标注（🔵 SKILL / 🟢 评分体系 / 🟡 数据集 / 🟤 Recipe / 🟠 评测工具链 / 🔴 产品代码 / 🟣 运行环境），细则见 [issue-attribution.md](./issue-attribution.md)
  - 报告里**同时**出现在两处：(a) 每条 finding 标注（行内 emoji）(b) 单独一节"归因分层汇总"聚合统计
  - 🟠 vs 🔴 判别：问题代码在 `sdk/skills/caw-eval/scripts/` 里 → 🟠；在 `src/app/` 或 `sdk/go/` 里 → 🔴
- L2 数据集 baseline 对比（如有）
- P0/P1/P2 按"风险严重度 × 发生频率 × 修复成本"排序
- 上线建议三选一：可上 / 有条件上 / 建议延期

### 9.3 报告结构
1. 总览（E2E + 任务完成率 + baseline 对比）
2. 逐 Case 评分（按 e2e_composite 从低到高）
3. 运行指标（时长/tokens/caw 命令/错误数/pact 效率，数据来自 session_metrics.json）
4. 逐 Case 详细分析
5. 按场景类型分析（transfer/swap/lend/dca/...）
6. 阶段瓶颈分析（S1/S2/S3）
7. 改进建议（P0/P1/P2）
8. **归因分层汇总（必做）** — 按 🔵/🟢/🟡/🟤/🟠/🔴/🟣 七层分组列出所有 finding，每层一个子表：`优先级 | Finding | 涉及 Case | 责任方 | Action Item`；末尾给每层的条目数和 P0/P1/P2 分布，便于快速看懂"问题主要出在哪层"
9. 上线建议

---

## 脚本分工

| 脚本 | 子命令 | 说明 |
|------|--------|------|
| `run_eval_cc.py` | `dispatch` | **本地端**：并行调度 N 台服务器，动态队列 |
| `run_eval_cc.py` | `run` | **服务器端**：headless `claude -p` 逐 item 执行（由 dispatch 调用） |
| `run_eval_cc.py` | `upload` | 批量上传 session 到 Langfuse 并关联 dataset run |
| `run_eval_cc.py` | `metrics` | 从 session 提取运行指标 |
| `run_eval_cc.py` | `score` | 调 `score_traces.py session` |
| `score_traces.py` | `session` | 读本地 .jsonl 评分 |
