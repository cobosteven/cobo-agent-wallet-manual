# Dataset Review — recipe-test 数据集样本审查规则

面向 `recipe-test-v2` 系列数据集（以及未来 `recipe-test-v3` 等后续版本）的样本审查与生成规则。

## 背景

`recipe-test-*` 数据集用于评测 agent 根据给定 recipe 构造并**提交** pact/tx 的能力。评测只看 agent 是否正确构造并调用 caw 的 `pact submit` / `tx call`，**不检查链上最终是否 confirmed**，链上 revert 不影响评分。

过去一批 AI 自动生成的样本里发现 9 类系统性问题，把它们抽成下面 10 条机械规则。新样本必须全部通过规则 1–10 才能入库；人工 reviewer 或 AI reviewer 对候选样本逐条打勾。

---

## 审查 Prompt（可直接喂给 AI reviewer）

```
你是 recipe-test 数据集审查员。给定候选样本的四段内容：
  - user_message（用户自然语言指令）
  - metadata.chain（目标链，如 eth_sepolia）
  - metadata.recipe（执行路径的参考 recipe，含合约地址白名单）
  - expected_output: {
      pact_hints: {expected_outcome, operation_type},
      success_criteria: [str],
      stage_criteria: {
        s1: { key_entities: {amount, token, chain}, constraints: [...] },
        s2: { dependencies: [...], steps: [...] },
        s3: { policy, pact_type }
      }
    }

按下列 11 项逐条给出 PASS / FAIL + 一句话理由。任何一项 FAIL 则样本不得入库。

1. 结构完整性
   - success_criteria 必须是 list[str]，每条 ≥ 20 字符、不能是单字符列表（list[char] 是入库 bug）
   - pact_hints.expected_outcome 非空

2. 金额一致性
   - 从 user_message 抽出所有 (数字, 单位)
   - 对应值在 **pact_hints、success_criteria、stage_criteria（含 s1.key_entities.amount 和
     s2.dependencies 描述文本）** 中出现的所有金额/raw amount（考虑 decimals）必须与
     user_message 严格匹配（USDC=1e6, WETH/ETH=1e18）
   - stage_criteria.s1.key_entities.amount 必须等于 user_message 的人类可读金额（如 "0.01"）
   - stage_criteria.s2.dependencies 的文本描述里出现的金额/raw 值必须与 user_message 一致
   - 同一金额的中文/英文/科学记数法表述要一致

3. 链地址白名单
   - 扫描 expected_output 里出现的所有 0x 地址
   - 每个地址必须属于 metadata.chain 对应 recipe 的合法地址，或该链的权威合约
     （官方 WETH9 / Circle USDC / Uniswap SwapRouter02 等）
   - 其他链的地址（Polygon WETH / Mainnet WETH / Base USDC 等）→ FAIL

4. 代币语义与 decimals
   - 代币命名必须一致（USDC vs USDT vs WETH9 vs Aave-WETH 不得混用）
   - raw amount 与 token decimals 匹配（USDC 6, WETH/ETH 18）

5. 操作语义 exact-in / exact-out
   - user_message 若含 "换到 X 个 Y" / "要拿到 X 个 Y" → 必须 exactOutputSingle，
     sc 需含 amountOut=X 和 amountInMaximum=quote*(1+slippage)
   - user_message 若含 "用 X 个 Y 换" → 必须 exactInputSingle，
     sc 需含 amountIn=X 和 amountOutMinimum=quote*(1-slippage)
   - 两者错配 → FAIL

6. success_criteria 必须有可机械比对的锚点
   - 至少包含：目标合约地址、函数选择器或签名、所有关键参数的值
   - 仅出现 "with right contract, calldata and parameters" 这类空话 → FAIL

7. 条件 / 多步逻辑覆盖
   - user_message 含 "如果…则…" / "达到阈值才…" → sc 必须覆盖两个分支
   - user_message 隐含多步动作（如 supply+borrow、wrap+swap）→ sc 要覆盖每一步，
     或在 metadata 标记 multi_step: true 并分步列出

8. 引用同步
   - expected_output 里如果引用了 user_message 原句（带引号），必须逐字匹配
   - 若修改了 user_message 的金额/token，**所有字段**（pact_hints、success_criteria、
     stage_criteria.s1.key_entities、stage_criteria.s2.dependencies）里的引用文本和数值
     必须同步更新，不得遗漏任何一处
   - stage_criteria 里涉及的 token 名称（如 WETH / USDC）必须与 user_message 的操作对象一致
     （例：用户卖 USDC，则 s2.dependencies 里的 check_balance 对象应为 USDC，不得写 WETH）

9. 金额大小阈值（防钱包见底 + 维持样本金额量级一致）
   - 单次 tx 的"实际本金消耗"换算到 USD 等值必须 ≤ $0.05
     - swap / transfer：按输入 token 数量计
     - supply / wrap：按 token 数量计（可回收，但按单次锁定算）
     - borrow / withdraw / unwrap / repay：不消耗本金，可放宽，但仍要校验不超过
       钱包典型持仓的 1/10
   - 不同 items 的金额应当在同一量级（跨度不超过 10×）
   - 金额下限：raw amount ≥ max(1, token_decimals/100)，否则接近 dust，agent 可能误判为无意义
   - 特殊值（type(uint256).max 表"全部"）须在 user_message 含 "全部 / all / max" 才合法

10. 评测标准是"提交成功" 不是"链上执行成功"
   - success_criteria 的动词必须是 "constructs / submits / calls" 等构造/提交动作
   - 禁止使用 "executes successfully on <chain>" / "tx is confirmed" /
     "transaction status=1" / "receipt shows success" 这类依赖链上状态的表述
   - 同样不得要求 agent "wait for confirmation" / "verify receipt"
   - pact_hints 里也要保持同样语气
   - 理由：评测只看 agent 能否构造并提交正确 pact/tx，链上 revert（如余额不足）不应计入失败

11. recipe_name 字段与 recipe 文本内 name 必须严格一致
   - metadata.recipe_name 必须等于 metadata.recipe 的 YAML frontmatter 中
     `name:` 后的值（不含 "name: " 前缀、不含引号）
   - 例如 recipe 以 `name: weth-wrap-unwrap` 开头，则 recipe_name 必须是
     `weth-wrap-unwrap`，不是 `"name: weth-wrap-unwrap"` 也不是 `"weth-wrap-unwrap.md"`
   - 生成脚本不要用 `recipe.split('\n')[0]` 当 recipe_name，必须剥掉 key

输出格式（严格）：
  - summary: 11 项结果汇总，例如 "1✅ 2❌ 3✅ ..."
  - failed: 列出每条 FAIL 项的编号 + 证据（引用原文）+ 建议改法
  - final_verdict: MERGEABLE / NEEDS_FIX
```

---

## 生成 Prompt（给新样本生成器）

在生成阶段就预防这些问题。给"生成新样本"的 AI 加下面强约束：

```
生成 recipe-test 候选样本时必须遵守：

- success_criteria 必须是 list[str]，写成 ["<criteria text>"]；
  禁止 list(criteria_text) 的拆字符写法
- 所有 0x 地址从 metadata.recipe 对应 chain 段取；禁止复制 recipe 里其他 chain 的地址
- 金额在 user_message / pact_hints / success_criteria 三处必须按 decimals 严格一致
  （USDC 6, WETH/ETH 18）
- user_message 用 "换到 X 个 Y" 表示精确收到 X，对应 exactOutputSingle；
  "用 X 个 Y 换" 表示精确卖出 X，对应 exactInputSingle
- success_criteria 必须含：目标合约地址 + selector / 函数签名 + 所有关键参数 raw 值；
  禁止仅写 "with right contract, calldata and parameters" 这类空话
- 条件 / 多步语义要在 success_criteria 中显式展开成每一步
- 单次 tx 本金消耗 USD 等值 ≤ $0.05；跨 item 金额量级差 ≤ 10×
- success_criteria 动词限于 "constructs / submits / calls"；
  禁止含 "executes successfully on <chain>" / "tx is confirmed" 这类链上状态表述
- metadata.recipe_name 必须等于 metadata.recipe 文本 frontmatter 的 name 值
  （剥掉 "name: " 前缀 + strip 两端空格/引号）

最终输出前自我检查一遍上面规则，不符合的条目重写。
```

---

## 常见坑位（case study）

基于 `recipe-test-v2` v1 → v3 的修复历史：

| 坑 | 触发规则 | 实例 |
|---|---|---|
| success_criteria 被拆成 list[char]（`list(str)` 入库 bug） | 规则 1 | 全部 9 条 |
| 金额 100× 错（user "借 0.01 USDC" vs sc `amount=1000000`） | 规则 2 | E2E-67QA |
| 跨链地址污染（Polygon WETH 混入 Sepolia 样本） | 规则 3 | E2E-95F8 |
| USDT / USDC 笔误 | 规则 4 | E2E-67QA |
| exact-out 被写成 exact-in（user 说"换到 0.1 USDC"，sc 写 `amountIn=..., amountOutMinimum=0.099`） | 规则 5 | E2E-95F8 |
| 空话 success_criteria（"with right contract, calldata and parameters"） | 规则 6 | E2E-6MUD / E2E-VWV7 |
| 条件逻辑未覆盖（user "if > X, unwrap all"，sc 没写 if/else） | 规则 7 | E2E-VWV7 |
| 引用文本与 user_message 金额不同步（pact_hints / success_criteria） | 规则 8 | E2E-95F8 |
| stage_criteria 金额未随 user_message 同步更新（s1.key_entities.amount 仍是旧值，s2.dependencies 描述金额不一致） | 规则 2 / 规则 8 | E2E-FMJW / E2E-L585 |
| stage_criteria.s2.dependencies 里 check_balance 对象写错 token（用户卖 USDC 却写 WETH） | 规则 8 | E2E-FMJW |
| ETH 类金额偏大（0.00008 ETH ≈ $0.18，跨度超 10×） | 规则 9 | E2E-CVC2 / E2E-6MUD |
| 要求"链上执行成功"（`transaction executes successfully on Sepolia`） | 规则 10 | E2E-CVC2 |
| recipe_name 带 `"name: "` 前缀（`split('\n')[0]` 没剥 key） | 规则 11 | E2E-6MUD |

---

## 推荐金额上限速查

按 1 ETH ≈ $2300 估算，单次消耗 USD 等值 ≤ $0.05 的常见 token 上限：

| token | decimals | 单次 ≤ $0.05 的数量 | 单次 ≤ $0.01（推荐） | raw 值参考（@$0.01） |
|---|---|---|---|---|
| USDC | 6 | 0.05 USDC | 0.01 USDC | 10000 |
| WETH / ETH | 18 | 0.0000217 ETH | 0.0000043 ETH | 5000000000000（=0.000005） |
| USDT / DAI | 6 / 18 | 0.05 USD 等值 | 0.01 USD 等值 | — |

## 运维约束

- 评测钱包 token 持仓与样本 recipe 引用的 token 必须匹配（例如 Aave Sepolia 用 Aave test token `0x94a9…`/`0xC558…`，Cobo CAW sandbox 钱包持有的是 Circle USDC `0x1c7D…` + 官方 WETH9 `0xfFf9…`）
- 不匹配时：
  - 若评测标准只看"提交成功"（本项目选定），**链上 revert 不影响评分**，样本可保留。
  - 若要求链上 confirmed，则需走 faucet / 预先 mint token；Aave Sepolia 部分 test token 不开放公开 `mint`（`isMintable=false`），需走 Aave staging 界面或联系 owner。
