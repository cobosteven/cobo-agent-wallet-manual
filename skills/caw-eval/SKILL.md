---
name: caw-eval
metadata:
  version: "2026.04.22.1"
description: |
  在本地 Mac 编排 CAW (Cobo Agentic Wallet) Agent 评测，并把 headless claude / openclaw agent
  dispatch 到远端服务器执行，最终产出评分数据和分析报告。
  Use when: 用户想运行 CAW 评测、跑评测、测试 Skill、评估 Agent 质量、
  生成评测报告，或说 "跑评测", "测评 CAW", "eval", "评分",
  "recipe 评测", "弱模型评测", "openclaw 评测", "模型兼容性测试"。
---

# CAW Eval

端到端评测 CAW Agent 质量：本地 Mac 作为调度器，dispatch 到远端服务器跑 headless claude
（标准 / recipe 评测）或 openclaw agent（弱模型兼容性评测），评分和报告都在本地完成。

## Step 0: 环境识别（必做）

```bash
[[ "$(hostname)" == *openclaw* ]] && echo "env=openclaw" || echo "env=local"
```

- `env=local`：继续。确保 `gcloud auth login` 已完成、IAP 通道可用。
- `env=openclaw`：停止。本 SKILL 是**本地调度器**，不能在 openclaw 服务器直接跑。
  请回到本地 Mac 终端后重新触发。

## 流程路由

根据用户意图选择评测方式，然后**读取对应的 reference 按步骤执行**：

| 用户说 | 评测方式 | 读取并执行 |
|--------|---------|-----------|
| "跑评测" / "测评 CAW" / "eval" / "评分" / "claude code 评测" | **标准模式 + CC headless** | → [run-eval-cc.md](./references/run-eval-cc.md) |
| "recipe 评测" / "recipe eval" | **Recipe 模式**（交易构建评测） | → [run-eval-recipe.md](./references/run-eval-recipe.md) |
| "recipe 对比评测" / "recipe 对比" | **Recipe 五模式对比**（OpenClaw / OC 真实 / CC+recipe / CC 无 recipe / CC 真实） | → [run-eval-recipe.md](./references/run-eval-recipe.md) |
| "real recipe" / "真实 recipe" / "live recipe" / "实测 recipe" / "backend recipe" | **真实 recipe 模式**（不注入 CAW_RECIPE_FILE，caw 调真实 backend） | → [run-eval-recipe.md](./references/run-eval-recipe.md) `cc_real_recipe` / `oc_real_recipe` |
| "弱模型验证" / "openclaw 评测" / "模型兼容性" / "doubao/minimax/gpt-5.4 评测" | **Openclaw 弱模型评测**（多台并行 dispatch） | → [run-eval-openclaw.md](./references/run-eval-openclaw.md) |

**默认走标准 CC 评测**（用户没明确说 "recipe" 或 "openclaw" 时）。

执行前的公共前置（SSH / gcloud / 服务器同步）：[common-execution.md](./references/common-execution.md)

---

## 概览

### 标准 / Recipe 评测（CC headless）

本地 Mac 用 `run_eval_cc.py dispatch` 并行调度 N 台服务器，每台跑 headless `claude -p`。

```
本地 dispatch → 动态队列（空闲服务器自动取下一个 item）
               → 远端 claude headless 跑任务
               → scp 拉回 session → 上传 Langfuse → 评分 → 报告
```

- 时间：14 case / 4 台 ≈ 30 分钟
- **服务器池**（详见 [run-eval-cc.md Step 2](./references/run-eval-cc.md)）：
  - CC + Recipe：5 台（test4-test8）
  - CC 无 Recipe / 标准 CC：4 台（070641 + test1-test3）
  - 两池互不干扰，Recipe 对比可并行
- 详细步骤：[run-eval-cc.md](./references/run-eval-cc.md)
- Recipe 三模式对比：[run-eval-recipe.md](./references/run-eval-recipe.md)

### Openclaw 弱模型评测

本地 Mac 用 `run_eval_openclaw.py dispatch` 并行调度多台 openclaw 服务器，每台串行跑
`openclaw agent`，session 直接上传 Langfuse，本地评分出报告。

```
本地 dispatch → 多台服务器 openclaw agent 跑任务 → 上传 Langfuse
             → 本地读 Langfuse trace 评分（LLM Judge subagent 并行）→ 报告
```

- 时间：14 case / 3 台弱模型 ≈ 1-3 小时（取决于模型）
- 详细步骤：[run-eval-openclaw.md](./references/run-eval-openclaw.md)

---

## 评分体系

### 标准模式

```
综合分 = task_completion × 0.3 + process_quality × 0.7
process_quality = S1(意图) × 0.15 + S2(Pact) × 0.45 + S3(执行) × 0.4
```

### Recipe 模式（交易构建评测）

```
综合分 = S1(意图) × 0.20 + S2(Pact) × 0.45 + S3(交易构建) × 0.35
S3 = tx_construction_correctness × 0.5 + recipe_adherence × 0.3 + tx_submission_success × 0.2
```

无 task_completion。仅评估交易是否被正确构建/提交，不评估链上执行结果。

所有分数 0-1。详见 [scoring.md](./references/scoring.md)。

## 问题归因（写报告时使用）

对每个 finding 按 7 层归类：🔵 被测 SKILL / 🟢 评分体系 / 🟡 数据集 / 🟤 Recipe / 🟠 评测工具链 / 🔴 产品代码 / 🟣 运行环境。细则见 [issue-attribution.md](./references/issue-attribution.md)。

## 数据集

| 数据集 | Case 数 | 场景 | 说明 |
|--------|:-------:|-----|------|
| `recipe-test-v3` | 7 | uniswap-swap / aave-lend / weth-wrap | Recipe 评测（推荐），统一 schema v2 |
| `standard-test-v3` | 7 | 同 recipe-test-v3 | 标准评测（推荐），同一份测试集 + 不同 eval_mode 做 A/B |
| `caw-agent-eval-seth-v2` | 14 | transfer / swap / lend / dca / ... | 旧 schema（pact_hints/stage_criteria），仅历史回放 |
| `caw-recipe-eval-seth-v1` | - | recipe 多步骤 | Sepolia 多步骤场景，部分 item 已部分新 schema |

- 默认 `recipe-test-v3`（recipe 模式）/ `standard-test-v3`（标准模式）
- `recipe-test-v3` 和 `standard-test-v3` 内容**完全一致**（只是 metadata.eval_type 不同），区别仅在运行时：
  - `--eval-mode recipe`：dispatch 注入 `CAW_RECIPE_FILE`，judge 评 tx 构建（不评链上）
  - `--eval-mode standard`：dispatch **不**注入 `CAW_RECIPE_FILE`（agent 自主 `caw recipe search`），judge 评全流程（含 task_completion）
- `--dataset-name` 可指定其他数据集
- 数据集管理：[dataset-management.md](./references/dataset-management.md)
- 数据集审查（11 条机械规则）：[dataset-review.md](./references/dataset-review.md)

## 服务器环境搭建

新建 openclaw 评测服务器（GCP 实例 / openclaw / caw / onboarding / 充值 / 验证）：
→ [server-setup.md](./references/server-setup.md)

## Scripts

| 脚本 | 用途 |
|------|------|
| `run_eval_cc.py` | CC 评测编排（dispatch / run / upload / score / metrics） |
| `run_eval_openclaw.py` | Openclaw 评测编排（dispatch / run / upload / pack） |
| `score_traces.py` | 评分管线（断言 + judge → 综合分 → Langfuse） |
| `judge_cc.py` | LLM-as-Judge（prompt 构建） |
| `assertions.py` | 结构化提取 + 门槛断言 |
| `eval_utils.py` | 公共工具（Langfuse 客户端 / 数据集 / 批量上传） |
| `upload_session.py` | session → Langfuse trace |
| `generate_dataset.py` | 数据集生成 |
| `validate_dataset.py` | 数据集 schema 校验 |
| `runtime_compliance.py` | 评测运行时合规自检 |
| `sync_to_servers.sh` | 服务器同步 + hash 校验 |
