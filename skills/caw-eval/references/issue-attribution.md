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
>
> **`反证回合` 三字段是硬性要求**：标题用了"X/N 全中"、"卡死/死透/永久"、"X 导致 Y" 这类绝对化措辞 → 对应字段必须填，没填不能发布 finding。详见下面 §1.5 反证回合（强制）。

```markdown
### 🔴 P1-X · 标题（≤ 30 字）

**现象**: <case> / <维度> / <观察到的异常值>
**证据**:
- `path/to/file.ext:LINE` — <函数/段落/字段一句话摘要>
- `session.jsonl / req_<ITEM>.txt`: `"<关键 excerpt 字符串>"`
- <至少 1 条 file:line / log excerpt / dataset field 锚点；否则整条 finding 降级为 🔍>
**根因**: 因为 <引自证据的事实 X>，所以 <产生行为 Y>；这导致 <后果 Z>

**反证回合**（含绝对化主张的 finding 强制；不适用时写 N/A 并说明理由，不能空缺）:
- **范围反例**（"X/N 全中"类）: 列具体 N case 名 + 从 (Total - N) 个未中的里挑 1 个，附支撑该 case 没中的证据锚点
- **时间核查**（"卡死/死透/永久/全部失败"类）: 评测时态 vs 至少 1 个后续时间点（≥ 1h 后）的 backend 实查对比
- **对照实验**（"X 导致 Y"类因果主张）: 找 paired case：变量 X 有 vs 无的同类型 case 各 ≥ 1，列两组在维度 Y 上的分数差

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
**反证回合**: N/A（finding 已降级为🔍 疑似，留待 Action Item 验证步骤完成后再走完整反证）
**Action Item**:
1. **验证步骤**: Read `path/to/file.ext` 确认 `symbol` / grep `<pattern>` 确认
2. ...
```

#### 1.5 反证回合（强制）— 绝对化主张触发

> **背景（2026-04-27 历史教训）**：gpt-5.4 评测主会话写出 3 条主结论事后被推翻——
> "logger pre-shell-expansion bug 17/17 全中" → 实测仅 4/17 真受影响（其他 13 case 的 inject path 在 scoring phase 已生效）；
> "TSS 死透 13/17" → 24h 后实查 backend，那批 tx 大部分已 Success（实际是延迟 4-24h，不是死透）；
> "并发污染 P0 影响 2 case" → 对照同类 case 单跑 vs 并发分数差是负向 / 不显著（并发未造成实质影响）。
>
> 共性：每条主张都用了**绝对化措辞**（"全中"、"死透"、"P0 导致"），但**只读了一个信源**就下结论。
> 三种被忽略的方法：第二信源（多源交叉）/ 时间演化（再次实查）/ 控制实验（paired 对比）。
> 加这一节强制把这 3 种方法嵌入 finding 流程，触发条件 = 标题/根因含绝对化主张关键词。

**触发关键词** → 必填对应反证字段（如多个触发条件叠加，多个字段都填）：

| 触发措辞 | 必填字段 | 检查方法 |
|---|---|---|
| "X/N 全中"、"全部失败"、"100% 受影响"、"所有 X 都"、"无一例外" | **范围反例** | 列具体 N case 名（不能用"前述"），从 Total-N 个未中的中挑 1 个，附该 case 没中的证据锚点（scores.json 反证 / 不同维度反证）|
| "卡死"、"死透"、"永久挂"、"再不"、"完全无法"、"backend 不动"、"100% 阻塞" | **时间核查** | 评测当时状态 + 至少 1 个后续时间点的 backend 实查（caw tx list / API 查 status / SSH 复查 trace）；列两个时间点的 status 对比 |
| "A 导致 B 低分"、"A 是主因"、"A 引起 Y"、"A 触发了 Y" | **对照实验** | 找 paired case：A 有 vs 无的同类型 case 各 ≥ 1（同 op_type / 同 chain / 同 difficulty）；列两组在 Y 维度的分数差和方向（正向支持因果 / 负向反对 / 不显著）|

**绝对化主张反证规则（任意触发即必填）**：

1. **没找到反例 / 时间没变化 / 分数差正向** → finding 强化，可保留绝对化措辞
2. **找到反例 / 时间确证变化 / 分数差负向或不显著** → 必须改写：
   - 范围措辞改为具体数字（"X/N 命中"、"占 X% 但 Y% 反例"）
   - 时间措辞改为快照（"评测窗内观察到 X 状态；24h 后实查 Y/Z 已恢复"）
   - 因果措辞改为相关（"X 与 Y 同期发生但实测分数差不显著，归因不应作为 P0 主因"）
3. **完全跑不出反证证据**（如复查需要等 24h、对照 case 不存在）→ 整条 finding 降级 🔍 疑似 + Action 第一项是"补做反证步骤"

**强制写法 vs 反例**:

| ❌ 错误（绝对化无反证） | ✅ 正确（带反证） |
|---|---|
| "logger bug 17/17 全中" | "logger bug 在 4/17 hit（dca-3rounds / dca-5rounds / uniswap-001/002）；其余 13 case 的 inject_backend_pact_specs 在 scoring phase 生效，证据 scores.json:dimensions.pact_structure_valid.reasoning='policies=2 条, conditions=2 条'（aave-001 反例）" |
| "TSS 死透 13/17 case TC=0" | "评测窗内 13/17 case 卡 pendingsignature → TC=0；24h 后 SSH 复查 caw tx list：8/13 已 Success/completed、3/13 broadcasting、2/13 Rejected——TSS 不是死透是延迟 4-24h" |
| "dispatch 并发污染 P0 影响 superfluid-2weis + uniswap-002" | "对照: superfluid-1weis(单跑 0.286) vs 2weis(并发 0.309) 差 +0.023; uniswap-001(单跑 0.309) vs 002(并发 0.252) 差 -0.057。并发与低分相关但分数差非主导级，归 P2 / 仅作为 dispatch 设计 bug 修，不应作为评测低分的主因素" |

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
- 🟤 Recipe 在 pact 模式 / e2e + recipe-source=real 评测里常见 P1（地址/ABI/Facts 结构问题）；在 recipe-source=empty 对照组里一般 0 条
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
