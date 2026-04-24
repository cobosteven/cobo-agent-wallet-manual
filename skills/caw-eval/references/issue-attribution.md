# 问题归因模型（7 层）

写评测报告时，对每个 finding 按下表归类。目的：让改进 action 直接指到"该改什么文件/资源"，避免跨团队扯皮和"都是 agent 模型问题"的笼统结论。

---

## 深度分析硬性要求（所有 finding 适用，不分层）

评测报告的价值 = 根因准确度。笼统/叙事式根因会直接误导后续修复，把工程时间花在假问题上。**所有 finding 必须证据驱动**，而不是基于对代码/数据结构的猜测或叙事推断。

### 1. 现象 → 证据 → 根因 → Action Item（四段式）

每条 finding 必须四段齐全；之前常见的"现象 → 根因 → Action"三段式 **不够**，被默认把"证据"环节跳过了。

| 段 | 内容 | 是否可省 |
|---|---|:---:|
| 现象 | case + 维度 + 观察到的异常值（分数/字段/报错/错误 tx） | ✗ |
| **证据** | **至少一条 `file:line` / `dataset field` / `log excerpt` 级别的直接引用，且本条引用必须是你本会话内真实 Read 过的内容** | ✗ |
| 根因 | 从证据推导的因果链，用"因为 X，所以 Y"结构 | ✗ |
| Action Item | 修到具体文件/字段，含预计影响的 case 集合 | ✗ |

#### 强制 Markdown 模板（每条 finding 必用）

> **写报告时严格按这个模板复制**。证据段是**独立一行**，不能塞进根因段里混合叙事；空行 = 必须加 🔍 疑似 前缀并改写根因和 Action。

```markdown
### 🔴 P1-X · 标题（≤ 30 字）

**现象**: <case> / <维度> / <观察到的异常值>
**证据**:
- `path/to/file.ext:LINE` — <函数/段落/字段一句话摘要>
- `session.jsonl / req_<ITEM>.txt`: `"<关键 excerpt 字符串>"`
- <至少 1 条 file:line / log excerpt / dataset field 锚点；否则整条 finding 降级为 🔍>
**根因**: 因为 <引自证据的事实 X>，所以 <产生行为 Y>；这导致 <后果 Z>
**Action Item**:
1. 改 `path/to/file.ext:LINE` 的 <XXX>
2. ...
```

**降级版（证据不足时必须用这个）**：

```markdown
### 🔍 🔴 P1-X · [疑似] 标题

**现象**: ...
**证据**: 未在本会话内定位到直接锚点（标题已加 🔍 前缀）
**根因**: 假设 <X>（待验证）
**Action Item**:
1. **验证步骤**: Read `path/to/file.ext` 确认 `symbol` / grep `<pattern>` 确认
2. ...
```

#### 写 finding 前的强制动作顺序

**不要凭记忆或模式匹配直接下根因**。按这个顺序走：

0. **写综合分数表 / 通过率 / "X/N pass" 之类断言前，必须本会话内 Read 过当前 run 的断言输出或 Langfuse score**（跑 `score_traces.py` 或 grep `assertions.py` gate 的 stdout）。**上一份报告的数字对本次无效**，模板可复用、数字绝不可复用
1. **先 grep session 原文**（`req_<ITEM>.txt` / session.jsonl / Langfuse trace observations）——agent 和 CAW 原始 stdout 都在这，**零成本就能拿到关键 error string**。典型 pattern:
   ```bash
   grep -B1 -A5 -iE "error|validation|failed|denied|exception|revert" req_<ITEM>.txt | head -40
   ```
2. **grep 到可疑字符串后**再去读对应源码（`src/app/` 后端 / `sdk/go/` CLI / `cobo-agentic-wallet-sandbox*/` SKILL / `scripts/` harness）定位 file:line
3. **确认 file:line 真的支持你的结论**后再开始写"根因"那一行
4. **第 0-3 步任何一步拿不到东西 → 直接用降级版模板（🔍 前缀 + 验证步骤 Action）**

### 2. 证据类型（按归因层映射）

| 归因层 | 可接受的证据形式 | 不可接受 |
|---|---|---|
| 🔵 SKILL | SKILL.md / references/*.md 的具体段落 + 行号 | "SKILL 应该说…"等推测口吻 |
| 🟢 评分体系 | scoring.md 条款 / judge_cc.py prompt 段落 / assertions.py gate 逻辑 + 行号 | "judge 可能判错了" |
| 🟡 数据集 | Langfuse dataset item 的 `input` / `expected_output` / `pact_expectation` / `success_criteria` 字段具体值（json dump） | "dataset 应该给了…" |
| 🟤 Recipe | `metadata.recipe` 正文具体段落（Facts/ABI/Policy Controls/Submission 区块）或 caw-cli recipe registry 内容 + 行号 | "recipe 大概意思是…" |
| 🟠 评测工具链 | `sdk/skills/caw-eval/scripts/` 下 run_eval_*.py / score_traces.py / assertions.py / upload_session.py 的函数体 + 行号 | "harness 可能 race" |
| 🔴 产品代码 | `cobo-agent-wallet/src/app/` 后端源码 / `sdk/go/` Go CLI 源码（含生成的 model_*.go）/ OpenAPI spec `sdk/generators/openapi.yaml` 的函数体 + 行号 | "后端应该返回…" / "CLI 可能 unmarshal 失败" |
| 🟣 运行环境 | 服务器端日志 / gcloud 错误 / IAP tunnel 状态 / caw CLI 部署版本 vs master 的 hash 差 | "可能是 OAuth 过期" |

> **🟠 vs 🔴 判别要点**：问题代码**在 `sdk/skills/caw-eval/scripts/` 里** → 🟠 评测工具链；**在 `src/app/` 后端或 `sdk/go/` CLI 里** → 🔴 产品代码。两者都**不是** 🟣（🟣 只管部署/环境 artifact）。

### 3. 当证据不足时的强制降级

**如果你找不到直接证据（Read 过且内容支撑结论），必须**：

- 在 finding 前加 `🔍 疑似` 前缀
- 根因段写成"假设 X（待验证）"
- Action Item 第一条必须是 **"验证步骤：Read XX 确认 YY"**
- 报告末尾单独小节列出所有 🔍 疑似 项，明确标注不是已确认根因

**不要**把"听起来合理"的叙事当定论写。读者会基于你的根因去改代码，写错了等于把工程时间导向假问题。

### 4. 委派读，不委派判断

需要覆盖多处代码/数据时并行派 Explore subagent Read 并返回"引文+行号"——由**主会话**合成结论。prompt 里明确要求：

- 必须返回 `file:line` 级别引用
- 结论不超过 200 字
- 遇到和预期不符的证据，原样返回，不自行解释

不要把"基于证据写结论"委派给 subagent——主会话才拥有完整上下文，能判断证据是否真的支持结论。

### 5. 提交前自检（mental diff）

报告写完后，对每条 finding 逐条过一遍：

- [ ] 证据段是否含 `file:line` / `field: value` / `log: "..."` 级别的锚点？
- [ ] 这个锚点我本会话确实 Read 了吗（不是"记忆中"或"之前看过"）？
- [ ] 锚点内容真的支持结论吗，还是只支持一部分？
- [ ] 有没有"未验证但顺口说出的断言"？（常见触发词：可能、应该、race、似乎、猜测）

任何一条不通过 → 降级到 🔍 疑似或删除。

### 6. 反模式（见过就要警惕）

- **"基于以往经验 / 记忆中 / 之前类似 case"** → 记忆会错位；重新 Read 当前代码。
- **"应该是 race / timing / 并发"** 却没读并发控制代码 → 99% 是别的原因，属诊断懒惰。
- **把同一类错误归到"🟠 harness 眼前幻象"然后转移到 🔵 SKILL** → 反模式之首（详见 [L94-97 反模式提醒](#优先级分布参考)）；🟠 出现真 bug 时必须归 🟠 到底。
- **结论先行，证据补齐** → 先读证据再下结论，不要反过来。
- **多个 finding 共用一段模糊叙事** → 拆成具体证据，每条 finding 独立成立。
- **照搬旧报告的数值表格 / 通过率 / P0-P1 清单** → 每个数字都必须本会话内 Read 过当前 run 的断言输出或 Langfuse score；模板可复用，数字绝不可复用。曾在 [eval_doubao_recipe_test_v3.md R5](~/.claude/projects/-Users-rocen-etl-cobo-agent-wallets/memory/eval_doubao_recipe_test_v3.md) 出现：把上次报告 "tx_submission_success 0/7" 抄过来，实测当轮 7/7 pass，导致 P1 finding 前提完全错。
- **把 LLM Judge 的 reasoning 当证据引用** → Judge 自己可能也没读原始 obs output（只看 session 摘要/自己上一步的推断）。引用 `judge_<ITEM>.json` 的 reasoning 等于二手叙事；要回到 Langfuse trace 的 observations / session 原文引用。同 [eval_doubao_recipe_test_v3.md R5](~/.claude/projects/-Users-rocen-etl-cobo-agent-wallets/memory/eval_doubao_recipe_test_v3.md) 的 SXNN calldata "265→264" Judge 误读案例。

---

## 七层

| 层 | 含义 | 对应 skill 文件 / 资源 | 修复责任方 |
|---|---|---|:---:|
| 🔵 **被测 SKILL**（agent 指令规则）| 告诉 agent 怎么做 | [cobo-agentic-wallet-sandbox/SKILL.md](../../cobo-agentic-wallet-sandbox/SKILL.md)、references/pact.md、error-handling.md、security.md | SKILL 作者 |
| 🟢 **评分体系**（scoring framework）| 告诉评分管道怎么判好坏 | [scoring.md](./scoring.md)、[judge_cc.py](../scripts/judge_cc.py) 的 prompt 文案、[assertions.py](../scripts/assertions.py) 的 gate 规则 | eval skill 作者 |
| 🟡 **数据集**（ground truth）| 告诉评分管道"正确答案是什么" | Langfuse dataset（`input` / `expected_output.operation_spec` / `pact_expectation` / `success_criteria`）+ [dataset-review.md](./dataset-review.md) | dataset 维护者 |
| 🟤 **Recipe / 领域知识**（domain knowledge）| 注入到 agent prompt 的协议/合约知识（地址、ABI、费率、preflight 规则、submission 模板） | dataset item 的 `metadata.recipe` 正文 / caw-cli recipe registry 内容 | 协议专家 / recipe 作者 |
| 🟠 **评测工具链**（harness / scripts）| 采集和处理数据的代码 | [run_eval_openclaw.py](../scripts/run_eval_openclaw.py)、[run_eval_cc.py](../scripts/run_eval_cc.py)、[score_traces.py](../scripts/score_traces.py)、[assertions.py parser](../scripts/assertions.py)、[upload_session.py](../scripts/upload_session.py) | eval skill 作者 |
| 🔴 **产品代码**（product source）| 被评测产品本身的源代码实现缺陷 | `cobo-agent-wallet/src/app/` 后端 FastAPI 源码 / `cobo-agent-wallet/sdk/go/` Go CLI 源码 / OpenAPI spec `sdk/generators/openapi.yaml` / 生成的 Python/TS SDK | 后端 / SDK 作者 |
| 🟣 **运行环境**（infrastructure）| 跑评测的物理/基础设施（**不含**产品源码本身）| GCP 实例 / openclaw runtime / 部署到服务器上的 CAW CLI 二进制版本 / Langfuse / gcloud IAP | 运维 |

> **🟡 vs 🟤 的关系**：两者都可能存在于同一个 Langfuse dataset item 里。🟡 是"判分锚点"（`expected_output` / `pact_expectation` — 评分管道用于比对 agent 行为是否正确），🟤 是"注入给 agent 的领域知识"（`metadata.recipe` — 影响 agent 决策但不直接参与评分比对）。修复人 / 失败模式 / 修改 PR 的责任方都不同，因此独立成层。

## 分辨原则

常见的混淆边界，用下面这些启发式判断：

1. **"agent 行为有问题"是 🔵 还是 🟢 还是 🟤？**
   - 如果 agent 真的做错了（如 policy 没加金额上限），**SKILL 指令文档**没写明 → **🔵 SKILL**
   - 如果 agent 按了 **recipe 里的说法**行事但 recipe 本身写漏 / 不醒目 / 地址过时 → **🟤 Recipe**
   - 如果 agent 做对了但 Judge 误判（如 contract_call 不需要 `token_in` 却被扣分）→ **🟢 评分体系** 或 **🟡 数据集**

2. **"Judge 判得不对"是 🟢 还是 🟡？**
   - Judge prompt / 维度描述 / 聚合公式 有问题 → **🟢 评分体系**（改 judge_cc.py）
   - Judge prompt 没问题，但 `expected_output` 里 ground truth 错了（如用了不适用的字段）→ **🟡 数据集**

3. **"recipe 相关问题"是 🟤 还是 🟡 还是 🔵？**
   - recipe 正文内容问题（地址过时、ABI 写错、Facts 段里 `target_in: X + Y` 只有一行散落说明没用醒目格式、Submission 模板不完整）→ **🟤 Recipe**
   - recipe 无错，但 dataset 的判分锚点（`expected_output.pact_expectation` / `success_criteria`）和实际 recipe 不一致 → **🟡 数据集**
   - recipe 正文清晰，agent 也读到了，但 SKILL 没教会 agent "拿到 recipe 后如何转化为 pact 字段" → **🔵 SKILL**

4. **"trace 字段看不见"是 🟠 还是 🟣？**
   - parser / logger / 归档流程有缺陷 → **🟠 评测工具链**
   - 后端 API 权限隔离 / 网络层限制 → **🟣 运行环境**

5. **"CAW 出错"是 🔵 / 🔴 / 🟣？**
   - 被测 skill 指令有误导，agent 顺着指令错 → **🔵 SKILL**
   - CAW 后端 `src/app/` 或 Go CLI `sdk/go/` 的**源码**有 bug（行为设计错误、schema 契约错位、异常 handler 逻辑错）→ **🔴 产品代码**（提 PR 到主仓 src/sdk）
   - 部署到服务器上的 caw 二进制**版本过旧或未同步**、服务器 openclaw runtime 缺依赖、gcloud IAP 失效 → **🟣 运行环境**（靠 sync_to_servers.sh / 升级部署解决，不用改源码）

   🔴 vs 🟣 判别要点：**修复需不需要改产品 source？** 需要 → 🔴；只要重新部署 / 同步 / 升级就能好 → 🟣。

## 报告里的建议用法

报告里**必须**同时做两件事：

1. **行内标注**：每条 finding 标注颜色 emoji（🔵/🟢/🟡/🟤/🟠/🔴/🟣），方便读者随文阅读时理解归因
2. **归因分层汇总（独立章节）**：按 7 层分组，每层一个子表聚合所有 finding

### 归因分层汇总章节模板

```markdown
## N. 归因分层汇总

### 🔵 被测 SKILL（条目数 / P0-P1-P2 分布）

| 优先级 | Finding | 涉及 Case | 责任方 | Action Item |
|:---:|---|---|:---:|---|
| P0 | policies 金额上限指令缺失 | 3/3 case | SKILL 作者 | 改 `cobo-agentic-wallet-sandbox/references/pact.md` 第 N 节补强约束 |
| P1 | recipe_search 后参数复用提示不够 | B7DJ | SKILL 作者 | 改 `recipe.md` 加"search→apply"闭环示例 |

### 🟢 评分体系（条目数 / 分布）

| 优先级 | Finding | 涉及 Case | 责任方 | Action Item |
|:---:|---|---|:---:|---|
| P1 | completion_conditions.threshold 判定口径不一致 | 4/7 | eval skill 作者 | 改 `scoring.md` + `judge_cc.py` prompt |

### 🟡 数据集（条目数 / 分布）
（如果没有，写"本轮无"）

### 🟤 Recipe / 领域知识（条目数 / 分布）

| 优先级 | Finding | 涉及 Case | 责任方 | Action Item |
|:---:|---|---|:---:|---|
| P1 | uniswap-v3-swap recipe 的 Facts 段里 `target_in: X + Y` 作为一行散落说明（非醒目 checklist） | 5/7 | recipe 作者 | `metadata.recipe` 末尾加 "Required pact fields" 章节 |

### 🟠 评测工具链（条目数 / 分布）

| 优先级 | Finding | 涉及 Case | 责任方 | Action Item |
|:---:|---|---|:---:|---|
| P0 | tx_submission 匹配模式过严 | 3/7 | eval skill 作者 | 改 `assertions.py` |

### 🔴 产品代码（条目数 / 分布）

| 优先级 | Finding | 涉及 Case | 责任方 | Action Item |
|:---:|---|---|:---:|---|
| P1 | 后端 `_validate_policy_rules` 把 Pydantic ValidationError 拼成字符串丢结构 | 6/7 | 后端 | 改 `src/app/modules/pact/service.py:1534` 用 `exc.errors()` |

### 🟣 运行环境（条目数 / 分布）
（如果没有，写"本轮无"）

### 分布速览

| 层 | 条目 | P0 | P1 | P2 | 主要责任方 |
|---|:---:|:---:|:---:|:---:|:---:|
| 🔵 SKILL | 2 | 1 | 1 | 0 | SKILL 作者 |
| 🟢 评分体系 | 1 | 0 | 1 | 0 | eval skill 作者 |
| 🟡 数据集 | 0 | - | - | - | - |
| 🟤 Recipe | 1 | 0 | 1 | 0 | recipe 作者 |
| 🟠 评测工具链 | 1 | 1 | 0 | 0 | eval skill 作者 |
| 🔴 产品代码 | 1 | 0 | 1 | 0 | 后端 / SDK 作者 |
| 🟣 运行环境 | 0 | - | - | - | - |
| **合计** | 6 | 2 | 4 | 0 | - |

→ 一句话结论："本轮问题以 🔵 SKILL + 🟠 工具链为主，修 P0 两条可覆盖 N 个 case。"
```

这样读者 3 分钟内能看懂："问题主要在哪层 / 谁要修 / 修哪些文件"。

## 优先级分布参考

正常情况下应该：
- 🔵 SKILL 通常占 P0/P1 大头（直接影响 agent 行为）
- 🟤 Recipe 在 recipe-mode 评测里常见 P1（地址/ABI/Facts 结构问题）；在非 recipe 评测里一般 0 条
- 🟡 数据集 偶尔出现 P0/P1（锚点设定与实际行为口径冲突）
- 🟢 评分体系 偶尔出现 P1（如维度定义偏差）
- 🟠 评测工具链 多为 P1/P2（harness bug 被修完后影响面减小）
- 🔴 产品代码 不固定（发现频率与评测覆盖面强相关）；每次出现都应提 PR 到主仓
- 🟣 运行环境 通常 P2/P3（短期靠 workaround 绕过）

如果某次评测所有 finding 全集中在 🟠 评测工具链，**先判断证据**：如果每条 🟠 finding 都有 `file:line` 级别代码引用证明 harness 确有 bug，就照实写，不要因为"🟠 太多看起来像幻象"就迁就分布把锅推给 🔵/🟡/🟤（这是历史上常见的误判模式）。只有在证据全是"眼前观察到的现象"而没有深入 harness 代码验证时，才应该重新评分并回看 🔵/🟡/🟤。

## 反模式

不要把 finding 归到：
- **"agent 模型能力不足"** — 太笼统。落到具体指令（🔵）或评分偏差（🟢）上
- **"数据不够"** — 如果是样本量，具体写"n=X 太小"并归 🟡；如果是信息缺失，归 🟠 parser
- **"评测不准"** — 必须拆到 🟢/🟡/🟠 的具体层
