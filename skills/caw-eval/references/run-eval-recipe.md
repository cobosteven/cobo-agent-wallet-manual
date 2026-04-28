# Recipe 评测：执行步骤

**本文件是 pact 模式（交易构建评测）的 Agent 执行指南。**
通过 `--recipe-source` 三种取值（`real / seed / empty`）对比量化 recipe 的价值，不评估链上执行结果。

> **术语**: 老 `--eval-mode recipe` ≡ 新 `--eval-mode pact`。
> 老 `--recipe-mode cc_with_recipe / cc_no_recipe / cc_real_recipe / openclaw / oc_real_recipe`
> ≡ 新 `--recipe-source seed / empty / real / seed / real`。

- 基础流程同 CC / Openclaw：见 [run-eval-cc.md](./run-eval-cc.md) / [run-eval-openclaw.md](./run-eval-openclaw.md)
- 公共环境配置：见 [common-execution.md](./common-execution.md)
- 本文档只说 Recipe 模式**特有的配置**

---

## 五种对比组合（=`--eval-mode pact` × `--recipe-source` × agent 类型）

| 组合 | flag 组合 | 注入机制 | recipe 内容 |
|------|----------|---------|------|
| **OC + seed** | `run_eval_openclaw.py --eval-mode pact --recipe-source seed` | dispatch 自动给 gateway 注入 systemd env `CAW_RECIPE_FILE=/tmp/caw-eval-recipe.json`，每 item 覆写 | 指定测试 recipe |
| **OC + real** | `run_eval_openclaw.py --eval-mode pact --recipe-source real` | dispatch 主动 teardown systemd drop-in（清残留），gateway 起 caw 时 env 干净 | caw recipe search 调真实 backend，含 `pact_template` 等完整字段 |
| **CC + seed** | `run_eval_cc.py --eval-mode pact --recipe-source seed` | 每 item 写 `/tmp/caw-eval-recipes/{run_name}/{item_id}.json`（`count=1` + 指定 recipe），`_run_single_cc_task` 启动 `claude` 前设进程 env | 指定测试 recipe |
| **CC + empty**（对照组） | `run_eval_cc.py --eval-mode pact --recipe-source empty` | 同上但写空 recipe（`count=0, results=[]`） | 空（agent search 拿到空结果） |
| **CC + real** | `run_eval_cc.py --eval-mode pact --recipe-source real` | 不写注入文件，子进程 env 显式 `pop("CAW_RECIPE_FILE")` 防残留 | caw recipe search 调真实 backend |

> 老 cli 仍接受：`--recipe-mode openclaw|oc_real_recipe|cc_with_recipe|cc_no_recipe|cc_real_recipe`（自动映射到新 `--recipe-source` 值）。

**关键**：所有组合 agent **行为链路一致**（都自主调 `caw recipe search`），差异只在 search 返回的内容。**不得**在 prompt 里禁止 search —— 否则对照组不成立。

**对照组意义**：
- `seed 分数 − empty 分数 ≈ 该 recipe（指定测试版本）提供的价值`
- `real 分数 − seed 分数 ≈ 真实 backend recipe（含 pact_template）相对测试 recipe 的增益`
- `real 分数 − empty 分数 ≈ 真实 backend recipe 端到端总价值`

**前置要求（OC + seed/real）**：
- `caw` 二进制须 ≥ 支持 `CAW_RECIPE_FILE` 的版本（D110257 已合入）
- dispatch 会自动 SSH 每台服务器 `systemctl restart openclaw-gateway`（需 ubuntu 免密 sudo，默认都有）
- 评测结束（非 fire-and-forget）自动 teardown，恢复 gateway 到原状态

---

## 评分公式

```
综合分 = S1(意图) × 0.20 + S2(Pact) × 0.45 + S3(交易构建) × 0.35
S2 = policies_correctness × 0.7 + completion_conditions_correctness × 0.3
     （若 pact_structure_valid 门槛不通过 → S2 直接 = 0）
S3 = tx_construction_correctness × 0.5 + recipe_adherence × 0.3 + tx_submission_success × 0.2
```

- 权重来源单一 source of truth：[scoring.md](./scoring.md)
- 无 `task_completion`（仅评构建 / 提交，不评链上执行）
- `recipe-source=empty` 模式：`recipe_adherence` 维度 N/A（score=0），S3 权重转给 tx_construction：
  `S3 = tx_construction_correctness × 0.7 + tx_submission_success × 0.3`

---

## 数据集

- **`caw-recipe-eval-seth-v1`**：Recipe 场景，Ethereum Sepolia
- 每个 item 的 `metadata.recipe` 字段含完整 recipe 内容

---

## Run Name 命名规范

run_name 模板：`eval-{cc|oc}-{model}-{eval-mode}-{recipe-source 别名}-{YYYYMMDD-HHMM}`

source 别名约定：
- `real` → `real-recipe`
- `seed` → `seed-recipe`
- `empty` → `no-recipe`

示例：

| 组合 | run_name 示例 |
|------|-------------|
| CC + seed | `eval-cc-sonnet-pact-seed-recipe-20260416-1200` |
| CC + empty | `eval-cc-sonnet-pact-no-recipe-20260416-1200` |
| **CC + real** | `eval-cc-sonnet-pact-real-recipe-20260416-1200` |
| OC + seed | `eval-oc-doubao-pact-seed-recipe-20260416-1200` |
| **OC + real** | `eval-oc-doubao-pact-real-recipe-20260416-1200` |

---

## Dispatch 命令（五种模式）

```bash
cd <repo>/cobo-agent-wallet
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11

DATASET_NAME=caw-recipe-eval-seth-v1
TS=$(date +%Y%m%d-%H%M)

# 组合 1: CC + seed (跑 SERVERS_CC_MAIN)
RUN_NAME=eval-cc-sonnet-pact-seed-recipe-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --eval-mode pact --recipe-source seed \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)

# 组合 2: CC + empty (对照组，跑 SERVERS_CC_CTRL，不要和 seed 同池)
RUN_NAME=eval-cc-sonnet-pact-no-recipe-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --eval-mode pact --recipe-source empty \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)

# 组合 3: CC + real (不注入 CAW_RECIPE_FILE，agent 调真实 backend；跑 SERVERS_CC_MAIN)
RUN_NAME=eval-cc-sonnet-pact-real-recipe-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --eval-mode pact --recipe-source real \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)

# 组合 4: OC + seed
MODEL_SHORT=doubao
MODEL_FULL=volcengine/doubao-seed-2.0-code
RUN_NAME=eval-oc-${MODEL_SHORT}-pact-seed-recipe-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_openclaw.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL" \
  --eval-mode pact --recipe-source seed \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)

# 组合 5: OC + real (不写 systemd drop-in，caw 调真实 backend)
RUN_NAME=eval-oc-${MODEL_SHORT}-pact-real-recipe-${TS}
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_openclaw.py dispatch \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --model "$MODEL_SHORT" \
  --model-full "$MODEL_FULL" \
  --eval-mode pact --recipe-source real \
  $(for s in "${SERVERS[@]}"; do echo --server "$s"; done)
```

---

## 评分（各模式）

评分命令同 [run-eval-cc.md Step 6-8](./run-eval-cc.md) / [run-eval-openclaw.md Step 3-4](./run-eval-openclaw.md)，但需加 `--eval-mode pact` + `--recipe-source <real|seed|empty>`：

```bash
# CC 模式（读本地 session）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py session \
  --session ~/.caw-eval/runs/$RUN_NAME/ \
  --dataset-name "$DATASET_NAME" \
  --eval-mode pact --recipe-source seed \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json

# OpenCLAW 模式（从 Langfuse 拉数据 + 后端 pact spec 回放）
.venv/bin/python sdk/skills/caw-eval/scripts/score_traces.py langfuse \
  --run-name "$RUN_NAME" \
  --dataset-name "$DATASET_NAME" \
  --eval-mode pact --recipe-source seed \
  --pact-specs-dir ~/.caw-eval/runs/$RUN_NAME/pact_specs \
  --dump-judge-requests ~/.caw-eval/runs/$RUN_NAME/judge_req.json
```

`--pact-specs-dir` 说明：dispatch 已在每个 item 跑完后自动把服务器端 `caw pact show` 输出归档到 `~/.caw-eval/runs/<run>/pact_specs/<pact_id>.json` 并 scp 回本地。传入此目录可让 score_traces 在遇到 shell 变量占位符（如 `--policies "$POLICIES"`）时用后端真实 spec 评分，而不是按字面判 0 分。详见 [harness_pact_logger_bug memory](~/.claude/projects/-Users-rocen-etl-cobo-agent-wallets/memory/harness_pact_logger_bug.md) 和 [dispatch 归档逻辑](../scripts/run_eval_openclaw.py)。

应用评分时同样加这些 flag。

---

## Recipe Search 诊断（必看）

OpenCLAW 模式下，recipe 只存在 `/tmp/caw-eval-recipe.json`，agent **必须**主动调用 `caw recipe search` 才能拿到内容。评分脚本会上传诊断指标：

| 指标 | 含义 | 预期 |
|------|------|------|
| `caw.recipe_searched` | 是否执行过 `caw recipe search`（0/1） | **OC + seed 模式应 = 1**，否则 agent 等于盲猜 |
| `caw.recipe_search_count` | 调用次数 | ≥ 1 |

**`recipe_searched=0` 而 E2E 较低** → 根因很可能是 agent 未 search，不是 recipe 内容有问题。写报告时区分：
- ❌ agent 没 search → SKILL / Prompt 指令问题（不能归咎于 recipe）
- ✅ agent search 了但结果不好 → recipe 内容或 agent 使用 recipe 的能力问题

---

## 三模式对比报告模板

三次 run 完成后生成对比报告：

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

## 3. 网络命令使用对比（诊断）

| 指标 | OpenCLAW | CC+Recipe | CC 无 Recipe |
|------|:-------:|:---------:|:----------:|
| recipe search | - | - | - |
| curl 调用 | - | - | - |
| web_search / web_fetch | - | - | - |

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

## Troubleshooting（Recipe 专有）

| 问题 | 解决 |
|------|------|
| recipe 内容未注入 | 确认 dataset item 的 `metadata.recipe` 字段非空 |
| agent 仍然执行了链上交易（不该） | 检查 prompt 是否含"交易构建模式"约束段 |
| `recipe_adherence` 全为 0 | `recipe-source=empty` 下正常（N/A）；有 recipe (`seed`/`real`) 模式应检查 judge prompt |
| openclaw 模式 `recipe_searched=0` 占比高 | SKILL.md "Recipe search first" 指令需强化；也可能模型本身跳过 search 倾向强 |
| dispatch 报 recipe_hash_match=false | 服务器 archive 和本地 dataset 字节不一致；重新跑 sync_to_servers.sh + dispatch |

其余通用问题见 [common-execution.md](./common-execution.md)。
