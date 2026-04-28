# CAW 评测快速上手

给想通过 Claude Code 跑 **CAW Agent 评测**（e2e 全流程 / pact 构造 / recipe 对比 / openclaw 弱模型兼容性）的同事的一页手册。核心流程：你只需要**和 Claude Code 说人话**，它会自动加载 `caw-eval` skill 把整条流水线跑完。

权威文档：[SKILL.md](./SKILL.md) / [references/run-eval-recipe.md](./references/run-eval-recipe.md)。本文档只覆盖使用者视角。

---

## 一、前置准备（一次性）

### 1. 代码和 Python 环境

```bash
git clone <repo-url>
cd cobo-agent-wallets/cobo-agent-wallet

# 用 uv 装依赖；.venv 名字不要改
uv sync
```

### 2. gcloud（访问远端评测服务器）

```bash
brew install --cask google-cloud-sdk
gcloud auth login                        # 一次即可
gcloud config set project openclaw-keq9xwm4

# 必须用 3.10+ 的 Python 驱动 gcloud
brew install python@3.11
# 建议写进 ~/.zshrc：
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
```

### 3. SSH 复用（避免每次 IAP 重认证）

在 `~/.ssh/config` 加：

```
Host *
  ControlMaster auto
  ControlPath ~/.ssh/ssh-%C
  ControlPersist 24h
  StrictHostKeyChecking no
```

### 4. Langfuse 凭证（评分上传用）

放到 `cobo-agent-wallet/sdk/skills/caw-eval/scripts/.env`（不在 git 里）。内含 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`。
```bash
# Langfuse Project - sandbox（数据集读写 + session trace + 评分写入）
LANGFUSE_PUBLIC_KEY=pk-lf-d652d99c-8d34-43b3-917f-e8fdbb108e9e
LANGFUSE_SECRET_KEY=sk-lf-37fa489a-70bb-4fbe-bb50-edd185c92d1e
LANGFUSE_HOST=https://langfuse.1cobo.com
```
### 5. 权限确认

- **GCP IAP SSH 权限**：评测前找 ops 申请；自检命令能列出实例就说明已加

**自检一句**：

```bash
cd cobo-agent-wallet
CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11 \
  gcloud compute instances list --project=openclaw-keq9xwm4 --filter="name~openclew" | head
```

能看到一堆 `luochong-openclew-dev-v1-*` 就 OK。如果已经在 §1.2 设了 `gcloud config set project openclaw-keq9xwm4`，可以省略 `--project` 参数。

---

## 二、启动 Claude Code

在 `cobo-agent-wallets/` 仓库根目录下启动 `claude`（或 VS Code 扩展）。仓库内的 `caw-eval` skill 会被自动识别。

---

## 三、怎么跟 Claude Code 对话

看到下列关键词 Claude Code 会自动加载 `caw-eval` skill 并走完整个流水线。直接说人话：

评测有两个正交维度：`--eval-mode {e2e, pact}`（要不要跑链上 + 评 task_completion）× `--recipe-source {real, seed, empty}`（recipe 来源 / 是否注入）。

| 想干的事 | 对 Claude Code 说 |
|---|---|
| **默认全流程评估**（最常用，agent 调真实 backend recipe） | "跑 e2e 评测" 或 "跑全流程评估" |
| **recipe 增益对比**（seed + empty + real 三 run，看 recipe 价值） | "跑 recipe 对比评测" |
| 只跑零 recipe baseline（看模型自身能力） | "跑 e2e 评测，recipe-source 为 empty" |
| 只评 pact 构造（注入 dataset recipe，不跑链上） | "跑 pact 评测，recipe-source 为 seed" |
| 用 openclaw + 弱模型 | "跑 openclaw 评测，模型用 doubao / minimax / gpt-5.4" |
| 指定数据集 | "跑评测，数据集 `<dataset-name>` 或贴 Langfuse URL" |
| 只评分已有 run | "评分 `<run-name>`" |
| 生成对比报告 | "给三个 run 写对比报告" |

### Claude Code 会做什么

1. **检查环境**（你在本地还是服务器上、gcloud 能不能用）
2. **拉最新 master**（有冲突会停下来问你）
3. **同步 skill/scripts/caw-cli** 到对应服务器池
4. **预检余额**（USDC/WETH/MATIC 等够不够跑完整集）
5. **并行 dispatch**（recipe 对比时跑 `seed` / `empty` / `real` 三 run；常规评测跑单 run）
6. **拉回 session + 上传 Langfuse + 评分**
7. **生成报告** 落到 `sdk/skills/caw-eval/reports/eval-report-<run>.md`

### 过程中它会问你什么

- **服务器池选哪组**：默认按模式自动选；要改时明说
- **余额不够**：它会给你地址让你补，或让它从富余的服务器转过去
- **冲突 / 失败**：中途报错会停下来问下一步（默认别让它自动 `git reset --hard` 之类）

### 预期时长

主力数据集 `caw-recipe-eval-v1`（17 case，base + polygon mainnet）：

| 评测 | 时间 |
|---|---|
| CC e2e（17 case / 3 台） | 30–60 min（含 DCA 多轮长任务，dispatch 默认 `--timeout 1200`）|
| CC pact + seed/empty（17 case / 3 台） | 30–45 min（不跑链上确认） |
| OpenCLAW + 弱模型（17 case / 3 台） | 1–3 h（模型慢） |

老数据集 `recipe-test-v3` / `standard-test-v3` 各 7 case，时长按比例 ~1/2。

多 run 并行时整体 ≈ 最慢那个。

---

## 四、完整例子

### 例 1：CC sonnet 全流程评估（最常用，agent 调真实 backend recipe）

> 执行一次 claude code 模型的 e2e 模式全流程评估，agent 调真实 backend recipe，数据集为 https://langfuse.1cobo.com/project/cmnfh467000zalj07czrh00vh/datasets/cmoe5412o02asnb074juhtz15/items

### 例 2：CC sonnet 零 recipe baseline（看模型自身能力）

> 执行一次 claude code 模型的 e2e 模式评估，recipe-source 为 empty（agent 拿不到 recipe），数据集 caw-recipe-eval-v1

### 例 3：openclaw 弱模型全流程评估

> 执行一次 gpt-5.4 模型的 e2e 全流程评估，agent 调真实 backend recipe，数据集 caw-recipe-eval-v1

> 例 3 中 `gpt-5.4` 可以换成 `doubao` / `minimax`。`claude code` 走 CC headless 路径，其他模型走 openclaw 路径，Claude Code 会自动判断分流到哪个服务器池。


---

## 五、结果在哪看

- **本地**：`~/.caw-eval/runs/<run-name>/`（session / metrics / judge requests / pact_specs）
- **报告**：`cobo-agent-wallet/sdk/skills/caw-eval/reports/eval-report-<run-name>.md`
- **Langfuse**：`https://langfuse.1cobo.com` → 左侧 sidebar 选组织 `Cobo` → 项目 `cobo-agentic-wallet-sandbox` → `Datasets`，进入对应数据集后看 Runs 列表，按 run_name 过滤

看 recipe 是否有价值：**`seed 综合分 − empty 综合分`** 就是 recipe 带来的增益（同一模型，控制 recipe 注入与否）。

---

## 六、常见情况

| 现象 | 处理 |
|---|---|
| `gcloud compute ssh` 卡住 / permission denied | 重新 `gcloud auth login`；确认在 `openclaw-keq9xwm4` 项目 |
| `gcloud compute instances list` 列出 0 items | active project 不对；自检命令带上 `--project=openclaw-keq9xwm4`，或先 `gcloud config set project openclaw-keq9xwm4` |
| Claude Code 不识别 "跑评测" | 确认在 repo 根目录启动；skill 位于 `cobo-agent-wallet/sdk/skills/caw-eval/` |
| 评分时 `$POLICIES` 被当字面判 0 分 | 已自动修复（`score_traces.py` 默认加载 `~/.caw-eval/runs/<run>/pact_specs/`）；如脚本旧仍报错，先 `git pull` 再重跑 |
| openclaw `recipe_searched=0` 占比高 | 不是 recipe 问题；是 agent 跳过 search，写报告时分开归因 |
| DCA / Superfluid 等长任务被 600s timeout 截断 | dispatch 加 `--timeout 1200`；caw-recipe-eval-v1 主力 case 推荐用 1200 |
| 想重跑某几个 case | "只重跑 case `<item-id>`"，它会带 `--item-id` |
| 余额不够 | 告诉它具体缺什么币、哪台服务器；它会从富余的服务器转过去 |

---

## 七、延伸阅读

- [SKILL.md](./SKILL.md) — skill 总入口和流程路由
- [references/run-eval-recipe.md](./references/run-eval-recipe.md) — recipe 模式权威文档（评分公式、三模式对比、报告模板）
- [references/run-eval-cc.md](./references/run-eval-cc.md) — CC headless 评测细节
- [references/run-eval-openclaw.md](./references/run-eval-openclaw.md) — openclaw 弱模型评测细节
- [references/common-execution.md](./references/common-execution.md) — 环境 / 同步 / 通用 troubleshooting
- [references/server-setup.md](./references/server-setup.md) — 新建评测机
- [references/scoring.md](./references/scoring.md) — 评分公式权威
- [references/issue-attribution.md](./references/issue-attribution.md) — 写报告时的归因分类
