"""
CAW 评测数据集的 Pydantic schema 定义。

用于 generate_dataset.py 生成 item 时强制结构约束，以及 judge 读 item 时的类型安全。

评分锚点分层：
- L3 Tx 构造  → expected.operation_spec （合约/selector/params）
- L2 Pact 设计 → expected.pact_expectation （allowed_chains/tokens/contracts/completion）
- L4 Recipe   → metadata.recipe （给 agent 的参考知识，独立于评分锚点）
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ── Tx 构造 ground truth（L3） ──────────────────────────────────────────────────


class ContractCallTx(BaseModel):
    """合约调用类交易（最常见）。agent 用 caw util abi encode 构造 calldata。"""

    step: int
    type: Literal["contract_call"]
    contract: str = Field(..., description="合约地址 0x + 40 hex")
    contract_label: str = Field("", description="人类可读合约名，如 'Aave V3 Pool (Sepolia)'")
    function: str = Field(..., description="函数签名，如 'approve(address,uint256)'")
    selector: str = Field(..., description="4-byte selector 0x + 8 hex")
    params: dict = Field(default_factory=dict, description="参数名 → 期望值")
    conditional: str | None = Field(
        None,
        description="条件执行说明，如 'allowance < amount'；无条件则为 None",
    )


class TransferTx(BaseModel):
    """代币转账类交易。caw 自动构造 calldata。"""

    step: int
    type: Literal["transfer"]
    token_id: str = Field(..., description="Cobo token_id，如 'SETH_USDC'")
    dst_address: str = Field(..., description="接收地址")
    amount: int = Field(..., description="raw amount (考虑 decimals)")


class SignMessageTx(BaseModel):
    """EIP-712 消息签名（不上链，用于 permit / off-chain order 等）。"""

    step: int
    type: Literal["sign_message"]
    destination_type: Literal["eip712"] = "eip712"
    typed_data_schema: dict = Field(..., description="EIP-712 结构化数据")


TxSpec = ContractCallTx | TransferTx | SignMessageTx


class OperationSpec(BaseModel):
    """L3 tx 构造层 ground truth。锚定链上客观事实，recipe 迭代时不变。"""

    protocol: str = Field("", description="协议名，如 'Aave V3' / 'Uniswap V3'")
    transactions: list[TxSpec]

    @model_validator(mode="after")
    def _check_steps_ordered(self) -> "OperationSpec":
        if not self.transactions:
            raise ValueError("operation_spec.transactions 不能为空")
        steps = [tx.step for tx in self.transactions]
        if steps != sorted(steps):
            raise ValueError(f"step 必须递增，得到: {steps}")
        return self


# ── Pact 设计期望（L2） ─────────────────────────────────────────────────────────


class PactPolicies(BaseModel):
    """Pact --policies JSON 的期望结构。"""

    allowed_chains: list[str] = Field(..., description="Cobo chain_id list，如 ['SETH']")
    allowed_tokens: list[str] = Field(..., description="Cobo token_id list，如 ['SETH_USDC']")
    allowed_contracts: list[str] = Field(
        default_factory=list, description="合约地址白名单（可从 operation_spec 汇聚）"
    )
    max_amount_per_tx: dict | None = Field(
        None,
        description="格式 {token, value}, raw amount 考虑 decimals",
    )


class PactCompletion(BaseModel):
    """Pact --completion-conditions JSON 的期望结构。"""

    type: Literal["tx_count", "amount_spent_usd", "time_elapsed", "token_amount_spent"]
    threshold: int | float = Field(..., description="类型对应的数值阈值")


class PactExpectation(BaseModel):
    """L2 pact 设计 ground truth。评测者的设计选择，和 operation_spec 对齐但不完全派生。"""

    intent_canonical: str = Field(
        ...,
        description="标准意图描述（给 judge 判语义对齐，不要求 agent 一字不差复述）",
    )
    policies: PactPolicies
    completion: PactCompletion


# ── Dataset item 整体 ─────────────────────────────────────────────────────────


class ItemInput(BaseModel):
    user_message: str


class ItemMetadata(BaseModel):
    """Dataset item 的 metadata。基础标签 + 评测场景标注 + Recipe 内容。"""

    id: str
    chain: str
    operation_type: str
    difficulty: Literal["L1", "L2", "L3"]
    category: str = ""
    tags: list[str] = Field(default_factory=list)

    eval_type: Literal["standard-eval", "recipe-eval"]
    should_refuse: bool = False

    # F3: 评测场景真实度标注
    wallet_paired: bool = False
    auto_approve_owner: bool = True

    # Recipe 上下文（可迭代，给 agent 的参考知识，不参与评分锚点）
    recipe_name: str | None = None
    recipe_version: str | None = None
    recipe: str | None = None
    variant: str | None = Field(
        None, description="同一 recipe_name 下的实现分支标识（multi-item 方案）"
    )


class ItemExpectedOutput(BaseModel):
    """统一 schema v2：operation_spec + pact_expectation 是唯一评分锚点。"""

    schema_version: int = 2
    operation_spec: OperationSpec
    pact_expectation: PactExpectation


class DatasetItem(BaseModel):
    """完整的 dataset item。上传到 Langfuse dataset 前必须通过此 schema 校验。"""

    id: str
    input: ItemInput
    expected: ItemExpectedOutput
    metadata: ItemMetadata

    @model_validator(mode="after")
    def _eval_type_consistency(self) -> "DatasetItem":
        """eval_type 完整性约束：
        - recipe-eval: 必须同时有 metadata.recipe + metadata.recipe_name
          （recipe 模式 dispatch 把这段文本注入到 CAW_RECIPE_FILE，缺失就坏掉）
        - standard-eval: 不附加约束。即使 metadata.recipe 残留也合法 ——
          运行时 CLI --eval-mode standard 不会注入 CAW_RECIPE_FILE，
          judge 标准模式分支也不读 recipe_content；recipe 字段只是历史参考。
        """
        if self.metadata.eval_type == "recipe-eval":
            if not self.metadata.recipe:
                raise ValueError(f"item {self.id}: eval_type=recipe-eval 但 metadata.recipe 缺失")
            if not self.metadata.recipe_name:
                raise ValueError(
                    f"item {self.id}: eval_type=recipe-eval 但 metadata.recipe_name 缺失"
                )
        return self

    @model_validator(mode="after")
    def _allowed_contracts_covers_operation_spec(self) -> "DatasetItem":
        """allowed_contracts 必须覆盖 operation_spec 里所有 contract_call 的合约地址。"""
        if not self.expected.operation_spec or not self.expected.pact_expectation:
            return self
        allowed = {c.lower() for c in self.expected.pact_expectation.policies.allowed_contracts}
        for tx in self.expected.operation_spec.transactions:
            if isinstance(tx, ContractCallTx):
                if tx.contract.lower() not in allowed:
                    raise ValueError(
                        f"item {self.id}: operation_spec contract {tx.contract} "
                        f"不在 pact_expectation.policies.allowed_contracts"
                    )
        return self


def validate_item(raw: dict) -> DatasetItem:
    """校验 item dict，失败抛 pydantic ValidationError。"""
    return DatasetItem.model_validate(raw)
