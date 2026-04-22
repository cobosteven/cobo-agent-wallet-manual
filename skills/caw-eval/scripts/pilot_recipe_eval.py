"""
Pilot 脚本：生成 Recipe 评测数据集，验证 operation_spec + pact_expectation 新方案端到端。

基于 Aave V3 Sepolia supply 场景，生成 2-3 个 pilot items 带完整 operation_spec / pact_expectation。
用途：
  1. 验证 schema 正确性
  2. 验证 judge prompt 渲染效果
  3. 上传 Langfuse dataset（可选）
  4. 跑真实评测（需要 run_eval_cc.py dispatch）

用法：
    # 仅本地 dry-run 生成并校验
    python pilot_recipe_eval.py --dry-run

    # 上传到 Langfuse
    python pilot_recipe_eval.py --dataset-name caw-recipe-eval-pilot-v0 --upload

    # 打印单个 item 的 judge prompt 预览
    python pilot_recipe_eval.py --show-judge-prompt E2E-pilot-aave-supply-approve-L2
"""

import argparse
import os
import sys
from pathlib import Path

# ── Aave V3 Sepolia 的权威常量 ────────────────────────────────────────────────
# 来自 dataset-review.md case study（0x94a9... / 0x6Ae4...）
# 注意：Aave Sepolia 测试 token 不是 Circle USDC，而是 Aave staging USDC
AAVE_USDC_SEPOLIA = "0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8"
AAVE_POOL_SEPOLIA = "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951"

RECIPE_AAVE_SUPPLY_CONTENT = """# Aave V3 Supply (Sepolia testnet)

## Use Case
Supply USDC to Aave V3 Pool on Ethereum Sepolia testnet.

## Fact
- Pool contract: 0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951
- USDC test token: 0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8 (6 decimals)
- Chain: Ethereum Sepolia (chain_id: SETH)

## Flow
1. Check `allowance(owner, pool)` via `caw util eth-call` on USDC token
2. If allowance < amount: call `approve(pool, amount)` on USDC token
3. After approve confirms: call `supply(asset, amount, onBehalfOf, referralCode)` on Pool

## ABI
- approve(address,uint256) — selector 0x095ea7b3
- supply(address,uint256,address,uint16) — selector 0x617ba037

## Risk Considerations
- Must verify reserve is active / not frozen before supplying
- Supply cap check via getReserveCaps
"""


# ── Pilot items 定义（3 个 variants，同一 recipe 不同金额或路径） ─────────────
PILOT_ITEMS: list[dict] = [
    # Item 1: 标准路径（approve + supply，0.01 USDC）
    {
        "id": "E2E-pilot-aave-supply-approve-L2",
        "input": {
            "user_message": "Supply 0.01 USDC to Aave V3 on Ethereum Sepolia (Aave staging USDC at 0x94a9... + Aave Pool at 0x6Ae4...)"
        },
        "expected": {
            "pact_hints": {"operation_type": "lend"},
            "operation_spec": {
                "protocol": "Aave V3",
                "transactions": [
                    {
                        "step": 1,
                        "type": "contract_call",
                        "conditional": "allowance < amount",
                        "contract": AAVE_USDC_SEPOLIA,
                        "contract_label": "USDC (Aave Sepolia)",
                        "function": "approve(address,uint256)",
                        "selector": "0x095ea7b3",
                        "params": {"spender": AAVE_POOL_SEPOLIA, "amount": 10000},
                    },
                    {
                        "step": 2,
                        "type": "contract_call",
                        "contract": AAVE_POOL_SEPOLIA,
                        "contract_label": "Aave V3 Pool (Sepolia)",
                        "function": "supply(address,uint256,address,uint16)",
                        "selector": "0x617ba037",
                        "params": {
                            "asset": AAVE_USDC_SEPOLIA,
                            "amount": 10000,
                            "onBehalfOf": "<agent_wallet>",
                            "referralCode": 0,
                        },
                    },
                ],
            },
            "pact_expectation": {
                "intent_canonical": "Supply 0.01 USDC to Aave V3 on Ethereum Sepolia",
                "policies": {
                    "allowed_chains": ["SETH"],
                    "allowed_tokens": ["SETH_USDC"],
                    "allowed_contracts": [AAVE_USDC_SEPOLIA, AAVE_POOL_SEPOLIA],
                    "max_amount_per_tx": {"token": "SETH_USDC", "value": 10000},
                },
                "completion": {"type": "tx_count", "threshold": 2},
            },
        },
        "metadata": {
            "id": "E2E-pilot-aave-supply-approve-L2",
            "chain": "eth_sepolia",
            "operation_type": "lend",
            "difficulty": "L2",
            "category": "pilot",
            "tags": ["aave", "supply", "sepolia", "pilot"],
            "wallet_paired": False,
            "auto_approve_owner": True,
            "variant": "supply_with_approve",
            "recipe_name": "aave-v3-supply",
            "recipe_version": "v1",
            "recipe": RECIPE_AAVE_SUPPLY_CONTENT,
        },
    },
    # Item 2: 更大金额（L3, 0.03 USDC）— variant 相同但金额变化
    {
        "id": "E2E-pilot-aave-supply-approve-L3",
        "input": {
            "user_message": "Supply 0.03 USDC to Aave V3 on Ethereum Sepolia (Aave staging USDC at 0x94a9... + Aave Pool at 0x6Ae4...)"
        },
        "expected": {
            "pact_hints": {"operation_type": "lend"},
            "operation_spec": {
                "protocol": "Aave V3",
                "transactions": [
                    {
                        "step": 1,
                        "type": "contract_call",
                        "conditional": "allowance < amount",
                        "contract": AAVE_USDC_SEPOLIA,
                        "contract_label": "USDC (Aave Sepolia)",
                        "function": "approve(address,uint256)",
                        "selector": "0x095ea7b3",
                        "params": {"spender": AAVE_POOL_SEPOLIA, "amount": 30000},
                    },
                    {
                        "step": 2,
                        "type": "contract_call",
                        "contract": AAVE_POOL_SEPOLIA,
                        "contract_label": "Aave V3 Pool (Sepolia)",
                        "function": "supply(address,uint256,address,uint16)",
                        "selector": "0x617ba037",
                        "params": {
                            "asset": AAVE_USDC_SEPOLIA,
                            "amount": 30000,
                            "onBehalfOf": "<agent_wallet>",
                            "referralCode": 0,
                        },
                    },
                ],
            },
            "pact_expectation": {
                "intent_canonical": "Supply 0.03 USDC to Aave V3 on Ethereum Sepolia",
                "policies": {
                    "allowed_chains": ["SETH"],
                    "allowed_tokens": ["SETH_USDC"],
                    "allowed_contracts": [AAVE_USDC_SEPOLIA, AAVE_POOL_SEPOLIA],
                    "max_amount_per_tx": {"token": "SETH_USDC", "value": 30000},
                },
                "completion": {"type": "tx_count", "threshold": 2},
            },
        },
        "metadata": {
            "id": "E2E-pilot-aave-supply-approve-L3",
            "chain": "eth_sepolia",
            "operation_type": "lend",
            "difficulty": "L3",
            "category": "pilot",
            "tags": ["aave", "supply", "sepolia", "pilot"],
            "wallet_paired": False,
            "auto_approve_owner": True,
            "variant": "supply_with_approve",
            "recipe_name": "aave-v3-supply",
            "recipe_version": "v1",
            "recipe": RECIPE_AAVE_SUPPLY_CONTENT,
        },
    },
]


def _langfuse_client():
    """加载 Langfuse client。从 scripts/.env 读凭证。"""
    from dotenv import load_dotenv

    scripts_dir = Path(__file__).parent
    load_dotenv(scripts_dir / ".env", override=False)

    from langfuse import Langfuse

    def _pick(specific: str, generic: str, default: str = "") -> str:
        return os.environ.get(specific) or os.environ.get(generic) or default

    host = _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST", "https://langfuse.1cobo.com")
    pk = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    sk = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")

    if not pk or not sk:
        raise RuntimeError("Langfuse 凭证未配置（PUBLIC_KEY / SECRET_KEY）")

    return Langfuse(public_key=pk, secret_key=sk, host=host, timeout=120)


def upload_to_langfuse(dataset_name: str) -> None:
    """上传 pilot items 到 Langfuse dataset。"""
    lf = _langfuse_client()
    # 先创建 dataset（如已存在不会报错）
    try:
        lf.create_dataset(name=dataset_name, description="Pilot dataset for Operation Spec v2")
        print(f"[INFO] 创建 dataset: {dataset_name}")
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
        print(f"[INFO] dataset {dataset_name} 已存在，追加 items")

    for item in PILOT_ITEMS:
        # Langfuse dataset item: input/expected/metadata
        md = dict(item["metadata"])
        md.setdefault("id", item["id"])
        lf.create_dataset_item(
            dataset_name=dataset_name,
            input=item["input"],
            expected_output=item["expected"],
            metadata=md,
        )
        print(f"[UPLOAD] {item['id']}")

    lf.flush()
    print(f"[DONE] 上传 {len(PILOT_ITEMS)} 个 items 到 {dataset_name}")


def show_judge_prompt(item_id: str) -> None:
    """打印某个 item 的 judge prompt（验证 operation_spec 渲染效果）。"""
    from judge_cc import build_judge_prompt

    item = next((i for i in PILOT_ITEMS if i["id"] == item_id), None)
    if not item:
        print(f"[ERROR] 找不到 item: {item_id}")
        print(f"可选: {[i['id'] for i in PILOT_ITEMS]}")
        sys.exit(1)

    prompt = build_judge_prompt(
        user_message=item["input"]["user_message"],
        expected=item["expected"],
        metadata=item["metadata"],
        assertion_context="pact_structure_valid=pass（mock）\ntx_submission_success=pass（mock）",
        best_pact_submit=None,
        eval_mode="recipe",
        recipe_content=item["metadata"].get("recipe", ""),
        session_text="(mock session content)",
    )
    print(prompt)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅本地校验 schema")
    parser.add_argument("--upload", action="store_true", help="上传到 Langfuse")
    parser.add_argument("--dataset-name", default="caw-recipe-eval-pilot-v0")
    parser.add_argument("--show-judge-prompt", metavar="ITEM_ID", help="打印 item 的 judge prompt")
    args = parser.parse_args()

    if args.show_judge_prompt:
        show_judge_prompt(args.show_judge_prompt)
        return 0

    # Schema 校验（总是跑）
    from schemas import validate_item

    passed, failed = 0, []
    for item in PILOT_ITEMS:
        try:
            validate_item(item)
            passed += 1
        except Exception as e:
            failed.append((item["id"], str(e)))
    print(f"[SCHEMA] {passed}/{len(PILOT_ITEMS)} PASS")
    for iid, err in failed:
        print(f"  [FAIL] {iid}")
        for line in err.splitlines()[:8]:
            print(f"         {line}")
    if failed:
        return 1

    if args.dry_run:
        print("[DRY-RUN] skipping upload")
        return 0

    if args.upload:
        upload_to_langfuse(args.dataset_name)
        return 0

    # 无 flag：默认打印 summary
    print("\n[SUMMARY] pilot items:")
    for item in PILOT_ITEMS:
        md = item["metadata"]
        n_tx = len(item["expected"]["operation_spec"]["transactions"])
        print(
            f"  {item['id']:50s} | {md['difficulty']} | variant={md.get('variant', '-'):25s} | tx={n_tx}"
        )
    print("\n要上传到 Langfuse: python pilot_recipe_eval.py --upload --dataset-name <name>")
    print(
        f"要预览 judge prompt: python pilot_recipe_eval.py --show-judge-prompt {PILOT_ITEMS[0]['id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
