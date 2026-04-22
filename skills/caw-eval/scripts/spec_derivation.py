"""
从 operation_spec / pact_expectation 派生人类可读的 judge 输入。

设计意图：
- operation_spec 是结构化 ground truth（合约、selector、params）
- judge prompt 需要易读的成功标准清单
- 这些文本是 judge 内部使用，不入库、不依赖 recipe 内容
"""

from typing import Any


def derive_intent_canonical(
    user_message: str,
    metadata: dict,
    operation_spec: dict | None,
    pact_expectation: dict | None,
) -> str:
    """为 S1 judge 派生"标准意图参考"。

    优先级:
      1. pact_expectation.intent_canonical（显式写好的）
      2. 从 metadata + operation_spec 合成一个基础版本
      3. 兜底回 user_message
    """
    if pact_expectation and pact_expectation.get("intent_canonical"):
        return str(pact_expectation["intent_canonical"])

    parts = []
    op_type = metadata.get("operation_type", "")
    if op_type:
        parts.append(op_type)

    if operation_spec:
        protocol = operation_spec.get("protocol", "")
        if protocol:
            parts.append(f"via {protocol}")

    chain = metadata.get("chain", "")
    if chain:
        parts.append(f"on {chain}")

    if parts:
        return f"User wants to: {' '.join(parts)}. Raw: {user_message}"
    return user_message


def derive_success_criteria(operation_spec: dict | None) -> list[str]:
    """从 operation_spec 派生 agent 应达成的 tx 构造清单（人类可读）。

    返回每行一条规范，按 step 顺序。例:
      ["step 1 (if allowance<amount): call approve(address,uint256) on 0x94a9... with spender=0x6Ae4..., amount=10000",
       "step 2: call supply(...) on 0x6Ae4... with asset=0x94a9..., amount=10000, ..."]
    """
    if not operation_spec:
        return []

    transactions = operation_spec.get("transactions", [])
    lines: list[str] = []
    for tx in transactions:
        step = tx.get("step", "?")
        tx_type = tx.get("type", "")
        conditional = tx.get("conditional")
        prefix = f"step {step}"
        if conditional:
            prefix += f" (if {conditional})"

        if tx_type == "contract_call":
            fn = tx.get("function", "?")
            contract = tx.get("contract", "?")
            contract_label = tx.get("contract_label", "")
            params = tx.get("params", {})
            contract_str = f"{contract}" + (f" ({contract_label})" if contract_label else "")
            params_str = ", ".join(f"{k}={_fmt_param(v)}" for k, v in params.items())
            lines.append(
                f"{prefix}: call {fn} on {contract_str}"
                + (f" with {params_str}" if params_str else "")
            )
        elif tx_type == "transfer":
            token = tx.get("token_id", "?")
            dst = tx.get("dst_address", "?")
            amount = tx.get("amount", "?")
            lines.append(f"{prefix}: transfer {amount} {token} to {dst}")
        elif tx_type == "sign_message":
            schema = tx.get("typed_data_schema", {})
            primary = schema.get("primary_type", "?")
            lines.append(f"{prefix}: sign EIP-712 typed message with primary_type={primary}")
        else:
            lines.append(f"{prefix}: [unknown type {tx_type!r}] {tx}")

    return lines


def derive_pact_checklist(pact_expectation: dict | None) -> list[str]:
    """从 pact_expectation 派生 pact 参数 checklist（给 S2 judge）。"""
    if not pact_expectation:
        return []

    lines: list[str] = []
    policies = pact_expectation.get("policies", {})
    chains = policies.get("allowed_chains", [])
    tokens = policies.get("allowed_tokens", [])
    contracts = policies.get("allowed_contracts", [])
    max_amount = policies.get("max_amount_per_tx")

    if chains:
        lines.append(f"policies.chain_in 至少覆盖: {chains}")
    if tokens:
        lines.append(f"policies.token_in 至少覆盖: {tokens}")
    if contracts:
        lines.append(f"policies.contract_in 至少覆盖: {contracts}")
    if max_amount:
        token = max_amount.get("token", "?")
        value = max_amount.get("value", "?")
        lines.append(f"policies 的 amount 限额应能容纳 {token}={value}（raw amount）")

    completion = pact_expectation.get("completion", {})
    ct = completion.get("type")
    th = completion.get("threshold")
    if ct and th is not None:
        lines.append(f"completion_conditions: type={ct}, threshold={th}")

    return lines


def _fmt_param(v: Any) -> str:
    """参数值格式化，地址类缩写、数字原样、其他 repr。"""
    if isinstance(v, str):
        if v.startswith("0x") and len(v) == 42:
            return f"{v[:6]}...{v[-4:]}"
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return repr(v)
