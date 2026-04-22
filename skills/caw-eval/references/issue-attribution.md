# 问题归因模型（5 层）

写评测报告时，对每个 finding 按下表归类。目的：让改进 action 直接指到"该改什么文件/资源"，避免跨团队扯皮和"都是 agent 模型问题"的笼统结论。

## 五层

| 层 | 含义 | 对应 skill 文件 / 资源 | 修复责任方 |
|---|---|---|:---:|
| 🔵 **被测 SKILL**（agent 指令规则）| 告诉 agent 怎么做 | [cobo-agentic-wallet-sandbox/SKILL.md](../../cobo-agentic-wallet-sandbox/SKILL.md)、references/pact.md、error-handling.md、security.md、recipe 内容 | SKILL 作者 |
| 🟢 **评分体系**（scoring framework）| 告诉评分管道怎么判好坏 | [scoring.md](./scoring.md)、[judge_cc.py](../scripts/judge_cc.py) 的 prompt 文案、[assertions.py](../scripts/assertions.py) 的 gate 规则 | eval skill 作者 |
| 🟡 **数据集**（ground truth）| 告诉评分管道"正确答案是什么" | Langfuse dataset（`input` / `expected_output.operation_spec` / `pact_expectation` / `metadata`）+ [dataset-review.md](./dataset-review.md) | dataset 维护者 |
| 🟠 **评测工具链**（harness / scripts）| 采集和处理数据的代码 | [run_eval_openclaw.py](../scripts/run_eval_openclaw.py)、[run_eval_cc.py](../scripts/run_eval_cc.py)、[score_traces.py](../scripts/score_traces.py)、[assertions.py parser](../scripts/assertions.py)、[upload_session.py](../scripts/upload_session.py) | eval skill 作者 |
| 🟣 **运行环境**（infrastructure）| 跑评测的物理/基础设施 | GCP 实例 / openclaw runtime / CAW CLI 二进制 / CAW 后端 API / Langfuse / gcloud IAP | 运维 + 后端 |

## 分辨原则

常见的混淆边界，用下面这些启发式判断：

1. **"agent 行为有问题"是 🔵 还是 🟢？**
   - 如果 agent 真的做错了（如 policy 没加金额上限），指令文档没写明 → **🔵 SKILL**
   - 如果 agent 做对了但 Judge 误判（如 contract_call 不需要 `token_in` 却被扣分）→ **🟢 评分体系** 或 **🟡 数据集**

2. **"Judge 判得不对"是 🟢 还是 🟡？**
   - Judge prompt / 维度描述 / 聚合公式 有问题 → **🟢 评分体系**（改 judge_cc.py）
   - Judge prompt 没问题，但 `expected_output` 里 ground truth 错了（如用了不适用的字段）→ **🟡 数据集**

3. **"trace 字段看不见"是 🟠 还是 🟣？**
   - parser / logger / 归档流程有缺陷 → **🟠 评测工具链**
   - 后端 API 权限隔离 / 网络层限制 → **🟣 运行环境**

4. **"CAW 出错"是 🔵 还是 🟣？**
   - 被测 skill 指令有误导，agent 顺着指令错 → **🔵 SKILL**
   - CAW CLI 本身 bug、后端返回异常 → **🟣 运行环境**（提 issue 给 CAW 产品）

## 报告里的建议用法

在报告的"改进建议"章节按这 5 层分组，每条 finding 标颜色 emoji。示例：

```markdown
## 改进建议

### 🔵 被测 SKILL
- **P0** policies 金额上限指令缺失（3/3 case 未加 deny_if.amount_gt）
  → 改 `cobo-agentic-wallet-sandbox/references/pact.md` 第 N 节补强约束

### 🟡 数据集
- **P1** recipe-test-v3.1 3 个 contract_call case 都在 `pact_expectation.policies` 里列了 `allowed_tokens`，但 contract_call 型 policy 不适用该字段
  → dataset 维护者删除这些 item 的 `allowed_tokens` 字段

### 🟠 评测工具链
- **已修** logger 记录 pre-shell-expansion argv → 加 `inject_backend_pact_specs` + `--pact-specs-dir`
```

## 优先级分布参考

正常情况下应该：
- 🔵 SKILL 和 🟡 数据集 通常占 P0/P1 大头（直接影响评测结果）
- 🟢 评分体系 偶尔出现 P1（如维度定义偏差）
- 🟠 评测工具链 多为 P1/P2（harness bug 被修完后影响面减小）
- 🟣 运行环境 通常 P2/P3（短期靠 workaround 绕过）

如果某次评测所有 finding 全集中在 🟠 评测工具链，多半是"眼前看到的问题"都是 harness 侧，应该回头重新评分后看**实际**的 🔵/🟡 问题。

## 反模式

不要把 finding 归到：
- **"agent 模型能力不足"** — 太笼统。落到具体指令（🔵）或评分偏差（🟢）上
- **"数据不够"** — 如果是样本量，具体写"n=X 太小"并归 🟡；如果是信息缺失，归 🟠 parser
- **"评测不准"** — 必须拆到 🟢/🟡/🟠 的具体层
