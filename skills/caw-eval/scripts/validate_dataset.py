"""
Dataset item schema 校验工具（I1 / 阶段 1 验收）。

用法：
    python validate_dataset.py --dataset-name caw-recipe-eval-v2-pilot
    python validate_dataset.py --from-generate  # 校验 generate_dataset.py 的 DATASET_ITEMS

用途：
    - 新建 / 修改 dataset 前本地自检
    - CI 流程里跑一次，违规 PR 不能合并
"""

import argparse
import sys

from pydantic import ValidationError


def _load_from_langfuse(dataset_name: str) -> list[dict]:
    """从 Langfuse 拉指定 dataset 的全部 items。"""
    from eval_utils import get_dataset_items

    items = get_dataset_items(dataset_name)
    # eval_utils 返回的 dict 格式和 generate_dataset 输出略有差异，标准化一下
    normalized: list[dict] = []
    for it in items:
        # eval_utils 把 input 展平了，要还原成 {input: {user_message}, expected: {...}, metadata: {...}}
        if "input" in it and isinstance(it["input"], dict):
            normalized.append(it)
        else:
            normalized.append(
                {
                    "id": it.get("id", ""),
                    "input": {"user_message": it.get("user_message", "")},
                    "expected": {
                        "pact_hints": it.get("pact_hints", {}),
                        "success_criteria": it.get("success_criteria", ""),
                        "stage_criteria": it.get("stage_criteria", {}),
                        "operation_spec": it.get("operation_spec"),
                        "pact_expectation": it.get("pact_expectation"),
                    },
                    "metadata": {
                        "id": it.get("id", ""),
                        "chain": it.get("chain", ""),
                        "operation_type": it.get("operation_type", ""),
                        "difficulty": it.get("difficulty", "L1"),
                        "wallet_paired": it.get("wallet_paired", False),
                        "auto_approve_owner": it.get("auto_approve_owner", True),
                        "recipe_name": it.get("recipe_name"),
                        "recipe_version": it.get("recipe_version"),
                        "recipe": it.get("recipe"),
                        "variant": it.get("variant"),
                    },
                }
            )
    return normalized


def _load_from_generate() -> list[dict]:
    """直接从 generate_dataset.py 加载 DATASET_ITEMS，用于 upload 前校验。"""
    from generate_dataset import DATASET_ITEMS

    out: list[dict] = []
    for it in DATASET_ITEMS:
        # generate_dataset 里的 metadata 缺 id 字段，schema 需要，补上
        md = dict(it["metadata"])
        md.setdefault("id", it["id"])
        out.append(
            {
                "id": it["id"],
                "input": it["input"],
                "expected": it["expected"],
                "metadata": md,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="CAW dataset schema validator")
    parser.add_argument("--dataset-name", help="Langfuse dataset 名称")
    parser.add_argument(
        "--from-generate",
        action="store_true",
        help="直接校验本地 generate_dataset.DATASET_ITEMS（无需连 Langfuse）",
    )
    parser.add_argument("--strict", action="store_true", help="任一 FAIL 则 exit 1（CI 用）")
    args = parser.parse_args()

    if args.from_generate:
        items = _load_from_generate()
        source = "local generate_dataset.DATASET_ITEMS"
    elif args.dataset_name:
        items = _load_from_langfuse(args.dataset_name)
        source = f"Langfuse dataset {args.dataset_name}"
    else:
        parser.error("必须指定 --dataset-name 或 --from-generate")

    print(f"[validate] source={source}, items={len(items)}")

    from schemas import validate_item

    passed, failed = 0, []
    for item in items:
        try:
            validate_item(item)
            passed += 1
        except ValidationError as e:
            failed.append((item.get("id", "?"), str(e)))
        except Exception as e:
            failed.append((item.get("id", "?"), f"unexpected: {e}"))

    print(f"[result] PASS={passed} FAIL={len(failed)}")
    for iid, err in failed:
        print(f"  [FAIL] {iid}")
        for line in err.splitlines()[:5]:
            print(f"         {line}")

    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
