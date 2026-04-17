# Recipe 评测：执行步骤

**本文件是 Recipe 评测（交易构建模式）的 Agent 执行指南。**

Recipe 评测用于检验 recipe 内容是否好用、是否有问题。通过三种模式对比，量化 recipe 的价值。

---

## 概览

### 三种对比模式

| 模式 | `--recipe-mode` | 说明 |
|------|:---------------:|------|
| **OpenCLAW + recipe** | `openclaw` | recipe 注入到 prompt，在 openclaw 服务器执行 |
| **CC + recipe** | `cc_with_recipe` | recipe 注入到 prompt，在 Claude Code 执行 |
| **CC 无 recipe** | `cc_no_recipe` | 不注入 recipe，纯靠 agent 自身知识（对照组） |

### 评分公式

```
综合分 = S1(意图) × 0.20 + S2(Pact) × 0.45 + S3(交易构建) × 0.35
S3 = tx_construction_correctness × 0.5 + recipe_adherence × 0.3 + tx_submission_success × 0.2
```

无 Task Completion。仅评估交易构建，不评估链上执行。

### 数据集

- **`caw-recipe-eval-seth-v1`**：Recipe 场景，Ethereum Sepolia 测试链
- 每个 item 的 `metadata.recipe` 字段包含完整 recipe 内容
- recipe 内容直接注入到评测 prompt（有 recipe 模式）或不注入（无 recipe 模式）

---

## Step 1: 检查环境

```bash
export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"
caw status          # 确认 healthy=true, signing_ready=true
caw wallet balance  # 确认 SETH 有余额
```

---

## Run Name 命名规范

三种模式的 run_name **必须包含模式标识**，便于在 Langfuse 中区分：

| 模式 | run_name 示例 |
|------|-------------|
| CC + recipe | `eval-cc-recipe-with-sonnet-20260416-1200` |
| CC 无 recipe | `eval-cc-recipe-none-sonnet-20260416-1200` |
| OpenCLAW + recipe | `eval-oc-recipe-with-doubao-20260416-1200` |

命名格式：`eval-{环境}-recipe-{with|none}-{模型}-{时间戳}`

---

## Step 2: 生成评测 prompt（三种模式各跑一次）

```bash
cd <repo>/cobo-agent-wallet
TS=$(date +%Y%m%d-%H%M)

# 模式 1: CC + recipe
# run_name: eval-cc-recipe-with-sonnet-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py prepare \
  --dataset-name caw-recipe-eval-seth-v1 \
  --eval-mode recipe --recipe-mode cc_with_recipe

# 模式 2: CC 无 recipe
# run_name: eval-cc-recipe-none-sonnet-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py prepare \
  --dataset-name caw-recipe-eval-seth-v1 \
  --eval-mode recipe --recipe-mode cc_no_recipe

# 模式 3: OpenCLAW + recipe（通过 dispatch 子命令）
# 见 Step 2b
```

### Step 2b: OpenCLAW 模式（通过 dispatch）

```bash
DATASET_NAME=caw-recipe-eval-seth-v1
RUN_NAME=eval-oc-recipe-with-${MODEL_SHORT}-$(date +%Y%m%d-%H%M)

.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_openclaw.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL" \
  --eval-mode recipe --recipe-mode openclaw \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)
```

---

## Step 3: 执行评测

对每个 case 启动后台 Sonnet subagent（同标准评测 Step 3，参考 [run-eval-cc.md](./run-eval-cc.md) Step 3）。

注意：recipe 模式的 prompt 已包含"交易构建模式"约束，agent 会在交易提交成功后停止，不会继续轮询。

---

## Step 4-8: 收集 → 上传 → 评分 → 报告

与标准评测流程相同（参考 [run-eval-cc.md](./run-eval-cc.md) Step 4-9），但评分时需加 `--eval-mode` 和 `--recipe-mode`：

```bash
# 评分（以 CC + recipe 为例）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/{run_name}/ \
  --dataset-name caw-recipe-eval-seth-v1 \
  --eval-mode recipe --recipe-mode cc_with_recipe \
  --dump-judge-requests ~/.caw-eval/runs/{run_name}/judge_req.json

# 应用 judge 结果
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/{run_name}/ \
  --dataset-name caw-recipe-eval-seth-v1 \
  --eval-mode recipe --recipe-mode cc_with_recipe \
  --judge-results ~/.caw-eval/runs/{run_name}/judge_results.json \
  --report
```

---

## Step 9: 三模式对比报告

三次 run 完成后，生成对比报告。报告模板：

### 对比报告模板

```markdown
# Recipe 评测对比报告

## 1. 综合分对比

| Case | OpenCLAW + Recipe | CC + Recipe | CC 无 Recipe | 差值（有-无） |
|------|:-----------------:|:-----------:|:----------:|:------------:|
| ... | ... | ... | ... | ... |
| **平均** | ... | ... | ... | ... |

## 2. 各维度对比

| 维度 | OpenCLAW | CC+Recipe | CC 无 Recipe |
|------|:-------:|:---------:|:----------:|
| S1 意图 | - | - | - |
| S2 Pact | - | - | - |
| S3 交易构建 | - | - | - |
| tx_construction | - | - | - |
| recipe_adherence | - | - | N/A |
| tx_submission | - | - | - |

## 3. 网络命令使用对比

| 指标 | OpenCLAW | CC+Recipe | CC 无 Recipe |
|------|:-------:|:---------:|:----------:|
| 网络命令总数 | - | - | - |
| curl 调用 | - | - | - |
| web_search | - | - | - |
| web_fetch | - | - | - |
| recipe search | - | - | - |

## 4. Recipe 质量分析

- 有 recipe 分数高于无 recipe → recipe 有效
- 有 recipe 分数 ≈ 无 recipe → recipe 未提供额外价值
- 有 recipe 分数低于无 recipe → recipe 可能有误导信息

## 5. Recipe 问题清单（如有）

| Case | 问题 | 影响 | 建议修复 |
|------|------|------|---------|
| ... | ... | ... | ... |
```

---

## Troubleshooting

| 问题 | 解决 |
|------|------|
| recipe 内容未注入 | 确认 dataset item 的 `metadata.recipe` 字段非空 |
| agent 仍然执行了链上交易 | 检查 prompt 是否包含"交易构建模式"约束 |
| recipe_adherence 全为 0 | CC 无 recipe 模式下正常（N/A），有 recipe 模式应检查 judge prompt |
| agent 使用了 caw recipe search | prompt 已禁用，若仍使用说明 agent 未遵循约束，可在报告中标注 |
