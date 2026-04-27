# Openclaw 弱模型评测：执行步骤

**本文件是 Openclaw 弱模型评测的 Agent 执行指南。** 从本地 Mac 并行 dispatch 到多台 openclaw 服务器跑弱模型（minimax / doubao / gpt-5.4 等），本地评分出报告。

公共环境配置（gcloud / SSH / 服务器同步 / troubleshooting）见 [common-execution.md](./common-execution.md)。

---

## 流程概览

```
Step 1: 选服务器组 + 模型对齐检查
Step 1.5: 余额预检（SETH / USDC / WETH）
Step 1.9: sync_to_servers.sh 同步 skill / scripts / caw-cli
Step 2: dispatch（动态队列）→ 各台 SSH 执行 openclaw agent，session 直接上传 Langfuse
Step 3-4: 本地生成 judge prompt → CC subagent 评分 → 应用到 Langfuse
Step 5: Opus subagent 生成分析报告
```

Session 数据通过 Langfuse API 读取，**无需 scp 下载**。

**推荐：动态队列阻塞模式**（dispatch 不加 `--fire-and-forget`）
→ 本地等待所有 item 完成，快台多跑、慢台少跑。

**可选：流水线模式**（`--fire-and-forget` + `--watch`）
→ dispatch 立即返回，`score_traces.py --watch` 轮询新 trace 实时生成 judge req。

---

## Step 0: 环境识别 + gcloud 配置

参见 [common-execution.md 的 gcloud Python 兼容性段](./common-execution.md)。

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
```

`env=local` 才能跑本流程。然后 `export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11`。

---

## Step 1: 服务器池选择 + 模型对齐检查

> **Openclaw 服务器 sessions prune**: 9 台评测服务器每周日 03:00 UTC 由 `/etc/cron.d/openclaw-prune` 自动清理 sessions.json 孤儿，防止 `agents add` 超 30s 静默失败（参数：window-days=7, keep-backups=4）。脚本**只部署在服务器**：`~/.agents/skills/caw-eval/scripts/prune_openclaw_sessions.sh`（本地 repo 不保留，`sync_to_servers.sh` 已排除）。遇 `agents add` 超时 → SSH 上去 `sudo bash -c "sudo systemctl stop openclaw-gateway && bash /home/ubuntu/.agents/skills/caw-eval/scripts/prune_openclaw_sessions.sh --smoke-test"` 手动触发。需要改清理规则：从任一服务器 `sudo cat` 取回脚本 → 修改 → 用 `gcloud compute scp` 推回各台。

服务器按模型分组（每组 3 台，格式 `name:zone:project`）：

```bash
# ── minimax-m2.5 ──────────────────────────────────────────────────────────
SERVERS_MINIMAX=(
  "luochong-openclew-dev-v1-20260415-0253420-test1:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-025458-test2:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-025551-test3:asia-east2-c:openclaw-keq9xwm4"
)

# ── doubao ────────────────────────────────────────────────────────────────
SERVERS_DOUBAO=(
  "luochong-openclew-dev-v1-20260415-121017-test4:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-121154-test5:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-121311-test6:asia-east2-c:openclaw-keq9xwm4"
)

# ── gpt-5.4 ───────────────────────────────────────────────────────────────
SERVERS_GPT=(
  "luochong-openclew-dev-v1-20260415-121400-test7:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260415-124552-test8:asia-east2-c:openclaw-keq9xwm4"
  "luochong-openclew-dev-v1-20260318-070641:asia-east2-a:openclaw-keq9xwm4"
)

SERVERS=("${SERVERS_DOUBAO[@]}")   # 选目标模型对应那组
```

**重要**：不要混用不同模型组的服务器——结果无法归因。

### 模型对齐检查

并行读取 N 台 `openclaw status`，人工比对 model 字段完全一致：

```bash
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (echo "=== $name ==="
   gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:\$PATH; openclaw status 2>&1 | head -3'"
  ) &
done
wait
```

提取模型字段：

```bash
MODEL_FULL="<从 status 复制，如 volcengine/doubao-seed-2.0-code>"
MODEL_SHORT="<短标识，如 doubao>"
```

---

## Step 1.5: 钱包余额预检

并行查询 caw 钱包余额：

```bash
mkdir -p /tmp/oc-balance
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; caw wallet balance 2>&1'" > /tmp/oc-balance/$name.txt 2>&1
  ) &
done
wait

for f in /tmp/oc-balance/*.txt; do
  name=$(basename $f .txt | sed 's/luochong-openclew-dev-v1-//')
  python3 -c "
import json
raw = open('$f').read()
idx = raw.find('{')
if idx == -1: print(f'  {\"$name\"}: 无数据'); exit()
d = json.loads(raw[idx:])
for r in d.get('result', []):
    print(f'  {\"$name\":30s} {r[\"token_id\"]:12s} available={r[\"amount\"]:>24s}')
"
done
```

**最低余额要求**（Ethereum Sepolia 评测）：
- **SETH ≥ 0.1**（gas + swap / transfer 操作）
- **SETH_USDC ≥ 14**（DeFi 类 case 需要 USDC 做 deposit / bridge / stream）
- **SETH_WETH ≥ 0.1**（unwrap / Aave borrow / approve→pull 等需要 WETH 余额；缺 WETH 会导致 unwrap / Aave borrow 链上 Failed）

### 补充余额

- **SETH 不足**：从余额充裕的服务器 `openclaw agent` 转入，或 `caw faucet`
- **USDC 不足**：并行 swap（prompt 必须含"已授权操作，不需要确认"以避免卡在确认）：

```bash
MSG="把 0.005 ETH 换成 USDC（Ethereum Sepolia，Uniswap V3）。这是已授权操作，直接创建 pact 并执行，不需要确认。完成后告诉我拿到了多少 USDC 和交易 hash。"
mkdir -p /tmp/oc-swap
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; \
     openclaw agent --agent main --message \"$MSG\" 2>&1'" > /tmp/oc-swap/$name.txt 2>&1
  ) &
done
wait
```

swap 涉及 wrap→approve→swap 三步链上交易，单台约 2-5 分钟。

- **WETH 不足**：并行 wrap（调 WETH9.deposit() 把 SETH 转为 WETH，1:1 不计 gas）：

```bash
MSG="wrap 0.1 ETH 成 WETH（Ethereum Sepolia）。这是已授权操作，直接创建 pact 并执行，不需要确认。完成后告诉我交易 hash。"
mkdir -p /tmp/oc-wrap
for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  (gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
     -- "sudo su - ubuntu -c 'export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:\$PATH; \
     openclaw agent --agent main --message \"$MSG\" 2>&1'" > /tmp/oc-wrap/$name.txt 2>&1
  ) &
done
wait
```

---

## Step 1.9: 同步 skill / scripts / caw-cli 到服务器

见 [common-execution.md 的"服务器同步"段](./common-execution.md)。

**先 `git pull` 拉最新 master**（见 common-execution.md "前置：拉取最新 master"），再执行 sync，否则服务器上跑的是过时 skill/scripts。**不要 stash 本地未提交修改**；若 `git pull` 有冲突，停下来让用户手动解。

推荐：

```bash
export SERVERS_DOUBAO="${SERVERS_DOUBAO[*]}"   # 转空格分隔字符串
bash sdk/skills/caw-eval/scripts/sync_to_servers.sh --component all --verify --servers-env SERVERS_DOUBAO
```

---

## Step 2: Dispatch（并行执行评测）

```bash
cd <repo>/cobo-agent-wallet

DATASET_NAME=standard-test-v3   # 标准模式默认；recipe 模式改用 recipe-test-v3
RUN_NAME=eval-oc-${MODEL_SHORT}-$(date +%Y%m%d-%H%M)

.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_openclaw.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL" \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)
```

`dispatch` 子命令：
1. 从 Langfuse 拉 dataset items 进队列
2. **动态队列**（默认）：N 台 worker 各自从队列取 item，空闲服务器不等待；所有 item 完成即结束
3. 每台 SSH 执行 `openclaw agent`，session 直接上传 Langfuse
4. 本地日志：`~/.caw-eval/runs/$RUN_NAME/dispatch-logs/<server>-<item_id>.log`

### 选项

| flag | 含义 |
|------|------|
| （默认） | 动态队列 + 阻塞等待；适合正式评测 |
| `--fire-and-forget` | 静态预分配 + nohup 后台启动；SSH 立即返回，搭配 `--watch` 流水线 |
| `--static` | 静态预分配 + SSH 阻塞等待；调试用 |
| `--eval-mode recipe --recipe-mode openclaw` | Recipe 评测（见 [run-eval-recipe.md](./run-eval-recipe.md)） |
| `--timeout 900` | 单 task 超时（默认 600） |
| `--item-id E2E-01L1 E2E-06L1` | 只跑部分 item（失败重跑） |

**部分 item 失败**：dispatch 会 `exit 1` 并打印失败项 + 重跑命令。

---

## Step 3: 生成 judge requests（从 Langfuse 拉数据）

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
```

脚本自动：
1. 从 Langfuse 拉 dataset run 的所有 trace
2. 对每个 trace 拉 observations，重建 StructuredExtraction
3. 跑代码断言（pact gate、diagnostics）
4. 生成 judge prompt（**session 内容直接嵌入 prompt**，不依赖本地文件）

### 流水线模式（可选）：边跑边评分

dispatch 后台启动后，立即运行 watch：

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --watch \
  --expected-count 14 \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
# 每有新 trace 上传，自动生成 judge req 追加到文件；达到 expected-count 自动退出
```

---

## Step 4: LLM Judge 评分（CC subagent 并行）

读取 `judge_req.json`，对每个 request 启动后台 Sonnet subagent。**始终保持 4-5 个并行**。

```python
import json
run_dir = "~/.caw-eval/runs/{run_name}"
requests = json.loads(open(f"{run_dir}/judge_req.json").read())

for req in requests:
    Agent(
        model="sonnet",
        run_in_background=True,
        description=f"Judge {req['item_id']}",
        prompt=f"""{req['system_prompt']}

{req['prompt']}

将 JSON 评分结果（严格按格式）写入：{run_dir}/judge_{req['item_id']}.json"""
    )
```

**重要**：openclaw 模式下 prompt 已含完整 session 内容，subagent 无需 Read 任何文件。

### 合并 judge 结果

```bash
cd ~/.caw-eval/runs/{run_name}
python3 -c "
import json, glob
results = []
for f in sorted(glob.glob('judge_E2E-*.json')):
    results.append(json.loads(open(f).read()))
open('judge_results.json', 'w').write(json.dumps(results, indent=2, ensure_ascii=False))
print(f'merged {len(results)} judge results')
"
```

> 每条 judge result 必须含 `trace_id` **和** `item_id` 两个字段（见 [common-execution.md Troubleshooting](./common-execution.md)）。

### 应用评分到 Langfuse

```bash
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --judge-results ~/.caw-eval/runs/$RUN_NAME/judge_results.json \
  --report
```

---

## Step 5: 生成报告

主会话已有全部评测上下文，直接在主会话里写报告。

### 5.1 步骤
1. Read `~/.caw-eval/runs/{run_name}/judge_results.json` 按 e2e_composite 排序
2. **写 P0/P1 finding 之前**，先 grep 对应 case 的 session 原文（`req_<ITEM>.txt`）拿 error excerpt——这是最便宜的证据来源，比读源码快 10 倍：
   ```bash
   grep -B1 -A5 -iE "error|validation|failed|denied|exception|revert" \
     ~/.caw-eval/runs/{run_name}/req_<ITEM>.txt | head -40
   ```
3. 低分 case（任一维度 <0.7）再 Read `judge_req.json` 里对应 item 的 `session_text` / `pact_section` 追根因
4. 疑似**产品源码**（后端 / CLI）问题时 Read `cobo-agent-wallet/src/app/` 或 `cobo-agent-wallet/sdk/go/` 对应文件验证（归 🔴 产品代码）
5. 疑似 SKILL 指令缺陷时 Read `cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-sandbox/` 对应文件验证
   - Openclaw 模式 skill 路径是 `cobo-agentic-wallet-sandbox`（非 `-dev`）
6. 输出到 `cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-{run_name}.md`

### 5.2 分析要求
- **深度分析硬性要求（所有 finding 适用）**：见 [issue-attribution.md "深度分析硬性要求"](./issue-attribution.md#深度分析硬性要求所有-finding-适用不分层)
  - 四段式 **现象 → 证据（含 file:line/字段/log 锚点）→ 根因 → Action Item**
  - **每条 finding 必须严格按 [issue-attribution.md 强制 Markdown 模板](./issue-attribution.md#强制-markdown-模板每条-finding-必用)**（证据是独立一行，不能塞进根因段里混合叙事）
  - 证据必须本会话真实 Read 过；找不到证据的 finding 降级为 `🔍 疑似` 且首条 Action 是"验证步骤"（见 [降级版模板](./issue-attribution.md#强制-markdown-模板每条-finding-必用)）
  - 🔵/🟢/🟡/🟤/🟠/🔴/🟣 任意层的根因都要读相应证据，不要因层次分布"看起来不对"就跳层归因
- **问题归因（必做）**：每条 finding 按 7 层标注（🔵 SKILL / 🟢 评分体系 / 🟡 数据集 / 🟤 Recipe / 🟠 评测工具链 / 🔴 产品代码 / 🟣 运行环境），细则见 [issue-attribution.md](./issue-attribution.md)
  - 报告里**同时**出现在两处：(a) 每条 finding 标注（行内 emoji）(b) 单独一节"归因分层汇总"聚合统计
  - 🟠 vs 🔴 判别：问题代码在 `sdk/skills/caw-eval/scripts/` 里 → 🟠；在 `src/app/` 或 `sdk/go/` 里 → 🔴
- Sonnet baseline 对比（如有同数据集历史 run）
- P0/P1/P2 按"风险严重度 × 发生频率 × 修复成本"排序
- 上线建议三选一：可上 / 有条件上 / 建议延期

### 5.3 报告结构
1. 总览（E2E + 任务完成率 + baseline 对比）
2. 逐 Case 评分表（按 E2E 排序）
3. 逐 Case 详细分析（低分 case 深入，高分 case 一行总结）
4. 按场景类型分析（transfer/swap/lend/dca/...）
5. 阶段瓶颈分析（S1/S2/S3/TC）
6. 高频失败模式
7. 与基线对比分析
8. 改进建议（P0/P1/P2）
9. **归因分层汇总（必做）** — 按 🔵/🟢/🟡/🟤/🟠/🔴/🟣 七层分组列出所有 finding，每层一个子表：`优先级 | Finding | 涉及 Case | 责任方 | Action Item`；末尾给每层的条目数和 P0/P1/P2 分布，便于快速看懂"问题主要出在哪层"
10. 修复收益预测

### 5.4 Openclaw 模式特点（相比 CC 评测）
- session 数据在 Langfuse：通过 `judge_req.json`（含 session_text）获取，无需本地 .jsonl
- 无 `session_metrics.json`，报告省略"运行指标"章节

---

## 对比分析（Sonnet vs 弱模型）

| 情况 | 含义 | 行动 |
|------|------|------|
| Sonnet 过 + 弱模型也过 | Skill 兼容性好 | 上线质量有保障 |
| Sonnet 过 + 弱模型挂 | Skill 指令不够清晰 | 简化 Skill 指令 |
| Sonnet 也挂 | Skill 有 bug | 必须修 |

---

## 脚本分工

| 脚本 | 子命令 | 说明 |
|------|--------|------|
| `run_eval_openclaw.py` | `dispatch` | **本地端**：并行 SSH 到 N 台服务器，动态队列调度 item |
| `run_eval_openclaw.py` | `run` | **服务器端**：串行执行 + 直接上传 Langfuse（通常由 dispatch 调用） |
| `score_traces.py` | `langfuse` | **本地端**：从 Langfuse 拉 trace + observations 重建评分 |
