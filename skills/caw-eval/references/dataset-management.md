# 数据集管理

创建、选择、更新 CAW 评测数据集。当用户需要创建新数据集或修改测试用例时，按以下步骤操作。

---

## Dataset Structure (统一 schema v2)

每个 dataset item 的格式：

```json
{
  "id": "E2E-LQED",
  "input": {
    "user_message": "Swap 0.001 USDC to WETH on Sepolia using Uniswap V3 with 0.05% fee tier"
  },
  "expected_output": {
    "schema_version": 2,
    "operation_spec": {
      "protocol": "uniswap-v3",
      "transactions": [
        {
          "step": 1,
          "type": "contract_call",
          "contract": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
          "function": "approve",
          "selector": "0x095ea7b3",
          "params": {"spender": "0x3bFA...", "amount": "1000"}
        },
        {
          "step": 2,
          "type": "contract_call",
          "contract": "0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E",
          "function": "exactInputSingle",
          "selector": "0x04e45aaf",
          "params": {"...": "..."}
        }
      ]
    },
    "pact_expectation": {
      "intent_canonical": "Swap 0.001 USDC to WETH on Sepolia via Uniswap V3 with 0.05% fee tier",
      "policies": {
        "allowed_chains": ["SETH"],
        "allowed_tokens": ["SETH_USDC"],
        "allowed_contracts": ["0x1c7d...", "0x3bfa..."],
        "max_amount_per_tx": {"token": "SETH_USDC", "value": 1000}
      },
      "completion": {"type": "tx_count", "threshold": 2}
    }
  },
  "metadata": {
    "id": "E2E-LQED",
    "chain": "eth_sepolia",
    "operation_type": "swap",
    "difficulty": "L1",
    "category": "uniswap_v3",
    "tags": ["uniswap", "swap", "sepolia"],
    "eval_type": "recipe-eval",
    "should_refuse": false,
    "recipe_name": "uniswap-v3-swap",
    "recipe": "<full recipe text from knowledge-hub>"
  }
}
```

- `eval_type=recipe-eval` 必须有 `metadata.recipe + metadata.recipe_name`
- `eval_type=standard-eval` 时 `metadata.recipe` 可有可无（标准模式 dispatch 不注入；judge 也不读）
- `expected_output.{operation_spec, pact_expectation, schema_version}` 三字段均必填
- 详见 [scripts/schemas.py](../scripts/schemas.py)

**Item ID 格式：** 早期用 `E2E-{scenario_id}{difficulty}`（如 `E2E-01L1`），新数据集用随机短码（如 `E2E-LQED`）

**测试网覆盖：**
- EVM: Ethereum Sepolia (`SETH`), Base Sepolia (`TBASE_SETH`)
- Solana: Devnet (`SOLDEV_SOL`)

---

## 已有数据集

| 数据集 | case 数 | schema | 说明 |
|--------|:-------:|--------|------|
| `recipe-test-v3` | 7 | v2 | **Recipe 评测推荐**，`metadata.eval_type=recipe-eval` |
| `standard-test-v3` | 7 | v2 | **标准评测推荐**，`metadata.eval_type=standard-eval`；与 recipe-test-v3 内容一致，做 A/B |
| `caw-agent-eval-seth-v2` | 14 | v1（旧） | 旧 `pact_hints/stage_criteria` 格式，仅历史回放，不再维护 |
| `caw-recipe-eval-seth-v1` | - | 部分 v2 | Recipe 多步骤；部分 item 缺 `metadata.eval_type`，validate_dataset 会报 FAIL |
| `caw-agent-eval-seth-v1` | 14 | v1（旧） | 旧版，expected_output 不完整 |
| `caw-agent-eval-v1` | 22 | v1（旧） | 主网场景，sandbox 环境无法执行大部分 case |

`schema = v2` 的数据集需通过 [scripts/validate_dataset.py](../scripts/validate_dataset.py) `--strict` 校验：
- `expected_output = {operation_spec, pact_expectation, schema_version: 2}` 三字段必填
- `metadata.eval_type` ∈ `{"standard-eval", "recipe-eval"}`
- `recipe-eval` 必须有 `metadata.recipe + metadata.recipe_name`

```bash
# 校验
cd <repo>/cobo-agent-wallet
.venv/bin/python sdk/skills/caw-eval/scripts/validate_dataset.py \
  --dataset-name recipe-test-v3 --strict
.venv/bin/python sdk/skills/caw-eval/scripts/validate_dataset.py \
  --dataset-name standard-test-v3 --strict

# 验证数据集可访问
.venv/bin/python sdk/skills/caw-eval/scripts/run_eval_cc.py prepare \
  --dataset-name recipe-test-v3
```

---

## 创建 / 重新上传数据集

当需要创建新数据集或重置现有数据集时：

```bash
cd <repo>/cobo-agent-wallet

# 预览（不上传）
.venv/bin/python sdk/skills/caw-eval/scripts/generate_dataset.py --dry-run

# 上传到默认数据集 caw-agent-eval-v1
.venv/bin/python sdk/skills/caw-eval/scripts/generate_dataset.py

# 指定不同的数据集名称
.venv/bin/python sdk/skills/caw-eval/scripts/generate_dataset.py \
  --dataset-name caw-agent-eval-v2

# 使用自定义 Langfuse 凭证
.venv/bin/python sdk/skills/caw-eval/scripts/generate_dataset.py \
  --public-key pk-lf-xxx --secret-key sk-lf-xxx
```

**注意：** 如果数据集已存在，重新上传会用相同 ID 覆盖已有 items（Langfuse SDK upsert 语义）。

---

## 修改测试场景

测试场景定义在 `scripts/generate_dataset.py` 的 `SCENARIO_RULES` 列表中。每条规则对应一类场景，包含多个难度变体（L1/L2/L3）。

**场景覆盖（22 个 item）：**

| ID | 场景 | 变体 | 说明 |
|----|------|------|------|
| 01 | transfer | L1/L2/L3 | ETH/ERC-20/SOL 转账 |
| 02 | dex_swap | L1/L2/L3 | Uniswap/指定路由/Jupiter Swap |
| 03 | lending | L1/L2/L3 | Aave 存款/存借/提取还款 |
| 04 | dca | L1/L2 | 日/周定投 |
| 05 | bridge | L1/L2 | 跨链/桥接+Swap |
| 06 | yield | L1/L2 | 利率查询/收益迁移 |
| 07 | multi_step | L1/L2 | 复合操作 |
| 08 | error_handling | L1/L2 | 余额不足/全仓操作 |
| 09 | edge_case | L1/L2/L3 | 不支持链/零地址/天文数字 |

**修改步骤：**

1. 编辑 `scripts/generate_dataset.py` 中对应的 `SCENARIO_RULES` 条目
2. 用 `--dry-run` 预览展开结果
3. 重新上传到 Langfuse

```python
# generate_dataset.py 中的规则结构
{
    "id": "01",
    "operation_type": "transfer",
    "category": "transfer",
    "description": "...",
    "eval_criteria": {          # S1-S3 评分基线（s1/s2/s3，各难度共享）
        "s1": { "operation_type": "transfer", "key_entities": [...] },
        "s2": { "steps": [...] },
        # ...
    },
    "variants": [               # 各难度变体
        {
            "difficulty": "L1",
            "user_message": "帮我把 0.001 ETH 转到 ...",
            "pact_hints": { "operation_type": "transfer" },
            # sN_overrides: 覆盖该难度特有的 eval_criteria 差异字段
        },
    ],
}
```

---

## Adding New Items Manually

如需向已有数据集追加单个 item（不重新生成全部），可用 Langfuse SDK 直接写入：

```python
from langfuse import Langfuse

lf = Langfuse()
lf.create_dataset_item(
    dataset_name="recipe-test-v3",
    id="E2E-10L1",
    input={"user_message": "..."},
    expected_output={"pact_hints": {...}, "success_criteria": "..."},
    metadata={"difficulty": "L1", "operation_type": "...", "category": "..."},
)
lf.flush()
```

---

## Langfuse Default Credentials

凭证通过 `scripts/.env` 文件配置（复制 `scripts/.env.example` 填入真实值，`.env` 已 gitignore）。

| 变量 | 说明 |
|------|------|
| `LANGFUSE_DATASET_HOST` | Langfuse 服务地址（默认 `https://langfuse.1cobo.com`） |
| `LANGFUSE_DATASET_PUBLIC_KEY` | Dataset project 公钥（见 `.env.example`） |
| `LANGFUSE_DATASET_SECRET_KEY` | Dataset project 私钥（见 `.env.example`） |

覆盖优先级：CLI 参数 `--public-key`/`--secret-key` > `LANGFUSE_DATASET_*` > `LANGFUSE_*`。
