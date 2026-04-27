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
    """从 Langfuse 拉指定 dataset 的全部 items（保留原始 expected_output / metadata 结构）。

    schema 校验需要完整的 operation_spec / pact_expectation / eval_type 等字段，
    不能用 eval_utils.get_dataset_items() 的扁平化输出（那是给评测 harness 用的）。
    """
    from eval_utils import get_langfuse_client

    lf = get_langfuse_client()
    dataset = lf.get_dataset(dataset_name)
    items = sorted(dataset.items, key=lambda i: i.id)
    normalized: list[dict] = []
    for item in items:
        inp = item.input if isinstance(item.input, dict) else {"user_message": item.input or ""}
        exp = item.expected_output if isinstance(item.expected_output, dict) else {}
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        item_id = meta.get("id") or item.id
        normalized.append(
            {
                "id": item_id,
                "input": inp,
                "expected": exp,
                "metadata": meta,
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
