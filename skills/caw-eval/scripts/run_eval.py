#!/usr/bin/env python3
"""
Script 2: 从 Langfuse 拉取测试数据，上传 session 数据

执行模式说明:
    本脚本负责数据集查询、session 收集和上传。测试用例的执行由 openclaw
    agent 通过 task subagent 完成（每个 item 独立 task，可并行）。

    数据集读写和 session 上传均通过 Langfuse SDK + API key 直接操作（无需 CAW 后端）。

用法:
    # 列出数据集中的所有 item（供 agent 读取后分发执行）
    python run_eval.py list --dataset-name caw-agent-eval-v1

    # 列出并输出 JSON 格式（方便 agent 解析）
    python run_eval.py list --dataset-name caw-agent-eval-v1 --format json

    # task 执行完成后，将该 item 的 session 文件收集到 run 目录
    python run_eval.py collect \
        --item-id E2E-01L1 \
        --run-dir ~/.caw-eval/runs/eval-run-20260407

    # 上传单个 session 文件并关联到 Langfuse run
    python run_eval.py upload \
        --session /path/to/session.jsonl \
        --dataset-name caw-agent-eval-v1 \
        --item-id E2E-01L1 \
        --run-name eval-run-20260407

    # 批量上传 run 目录下所有 session（文件名 stem = item_id）
    python run_eval.py upload \
        --run-dir ~/.caw-eval/runs/eval-run-20260407 \
        --run-name eval-run-20260407

环境变量:
    LANGFUSE_HOST         - Langfuse 服务地址
    LANGFUSE_PUBLIC_KEY   - Langfuse 公钥（数据集读写 + session 上传）
    LANGFUSE_SECRET_KEY   - Langfuse 私钥（数据集读写 + session 上传）
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# 自动加载同目录下的 .env（不覆盖已设置的环境变量）
load_dotenv(Path(__file__).parent / ".env", override=False)

# upload_session.py 的位置（与本脚本同目录）
_UPLOAD_SESSION_SCRIPT = Path(__file__).parent / "upload_session.py"

_DEFAULT_HOST = "https://langfuse.1cobo.com"


def get_langfuse_config() -> dict[str, str]:
    """Langfuse project credentials — used for both dataset reading and session upload.

    Priority: LANGFUSE_DATASET_* → LANGFUSE_* → .env file.
    """
    def _pick(specific: str, generic: str) -> str:
        return os.environ.get(specific) or os.environ.get(generic) or ""

    pub = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    sec = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")
    if not pub or not sec:
        print("[WARN] Langfuse credentials not set. "
              "Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY "
              "(or LANGFUSE_DATASET_PUBLIC_KEY + LANGFUSE_DATASET_SECRET_KEY) in .env or env vars.")
    return {
        "host": _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST") or _DEFAULT_HOST,
        "public_key": pub,
        "secret_key": sec,
    }


# Keep backward-compatible alias
get_dataset_langfuse_config = get_langfuse_config


def preflight_check() -> bool:
    """检查 session 上传所需的运行前提条件。"""
    if not _UPLOAD_SESSION_SCRIPT.exists():
        print(f"[PREFLIGHT ERROR] upload_session.py not found at: {_UPLOAD_SESSION_SCRIPT}")
        return False
    print("[PREFLIGHT OK] upload_session.py found")
    print("[INFO] Langfuse credentials read from LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY "
          "(or LANGFUSE_DATASET_* variants) in .env or env vars.")
    return True


def cmd_list(
    dataset_name: str,
    item_id: str | None,
    fmt: str,
) -> None:
    """
    列出数据集中所有 item（或指定 item），供 openclaw agent 读取后分发执行。
    """
    from langfuse import Langfuse

    cfg = get_dataset_langfuse_config()
    lf = Langfuse(
        public_key=cfg["public_key"],
        secret_key=cfg["secret_key"],
        host=cfg["host"],
    )
    dataset = lf.get_dataset(dataset_name)
    items = sorted(dataset.items, key=lambda i: i.id)
    if item_id:
        items = [i for i in items if i.id == item_id]

    if fmt == "json":
        output = [
            {
                "id": item.id,
                "user_message": item.input.get("user_message", ""),
                "operation_type": item.metadata.get("operation_type", ""),
                "difficulty": item.metadata.get("difficulty", ""),
                "chain": item.metadata.get("chain", ""),
                "success_criteria": item.expected_output.get("success_criteria", "") if item.expected_output else "",
            }
            for item in items
        ]
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(f"Dataset: {dataset_name}  ({len(items)} items)\n")
        for item in items:
            msg = item.input.get("user_message", "")
            op = item.metadata.get("operation_type", "")
            diff = item.metadata.get("difficulty", "")
            print(f"  [{item.id}] [{op}] [{diff}]")
            print(f"    {msg}")
            print()


def _extract_session_id(session_path: str) -> str:
    """从 JSONL 文件提取 session_id（第一个 type=session 事件）。"""
    try:
        with open(session_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("type") == "session":
                    return ev.get("id", "")
    except Exception:
        pass
    return Path(session_path).stem


def upload_session(
    session_path: str,
    skill_name: str = "cobo-agentic-wallet-sandbox",
) -> str | None:
    """
    通过 upload_session.py CLI 直接上传 session.jsonl 到 Langfuse。
    返回 session_id（作为 Langfuse trace_id），失败返回 None。

    Langfuse 凭据由 upload_session.py 从环境变量或 .env 文件读取。
    """
    session_id = _extract_session_id(session_path)
    if not session_id:
        print("    [UPLOAD ERROR] Cannot extract session_id from session file")
        return None

    if not _UPLOAD_SESSION_SCRIPT.exists():
        print(f"    [UPLOAD ERROR] upload_session.py not found at {_UPLOAD_SESSION_SCRIPT}")
        return None

    env = {**os.environ}

    try:
        result = subprocess.run(
            [sys.executable, str(_UPLOAD_SESSION_SCRIPT), session_path,
             "--skill", skill_name],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            print(f"    [UPLOAD] {result.stdout.strip()[:200]}")
        if result.returncode != 0:
            print(f"    [UPLOAD ERROR] exit={result.returncode} {result.stderr[:200]}")
            return None
        return session_id
    except subprocess.TimeoutExpired:
        print("    [UPLOAD ERROR] upload_session.py timed out")
        return None
    except Exception as e:
        print(f"    [UPLOAD ERROR] {e}")
        return None


def link_to_dataset_run(
    lf,
    dataset_name: str,
    item_id: str,
    run_name: str,
    trace_id: str,
) -> None:
    """将 Langfuse trace 关联到 dataset item run（Langfuse v4 API）。"""
    try:
        lf.api.dataset_run_items.create(
            run_name=run_name,
            dataset_item_id=item_id,
            trace_id=trace_id,
        )
        print(f"    [LINKED] trace={trace_id[:8]}... -> run={run_name} item={item_id}")
    except Exception as e:
        print(f"    [LINK ERROR] {e}")


def cmd_upload(
    session_path: str,
    dataset_name: str,
    item_id: str,
    run_name: str,
    skill: str,
) -> None:
    """上传 session 文件并关联到 Langfuse dataset run。"""
    from langfuse import Langfuse

    preflight_check()

    cfg = get_langfuse_config()
    lf = Langfuse(
        public_key=cfg["public_key"],
        secret_key=cfg["secret_key"],
        host=cfg["host"],
    )

    print(f"[INFO] Uploading session: {session_path}")
    trace_id = upload_session(session_path, skill)
    if trace_id:
        print(f"[INFO] trace_id: {trace_id}")
        if dataset_name and item_id:
            link_to_dataset_run(lf, dataset_name, item_id, run_name, trace_id)
    else:
        print("[ERROR] Upload failed")
        sys.exit(1)

    lf.flush()


def main() -> None:
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    # ── list subcommand ────────────────────────────────────────────────────────
    lp = sub.add_parser("list", help="列出数据集 items（供 agent 分发执行）")
    lp.add_argument("--dataset-name", default="caw-agent-eval-v1")
    lp.add_argument("--item-id", help="只列出此 item")
    lp.add_argument("--format", choices=["text", "json"], default="text",
                    help="输出格式（默认: text）")

    # ── upload subcommand ──────────────────────────────────────────────────────
    up = sub.add_parser("upload", help="上传 session 文件并关联到 Langfuse run")
    up.add_argument("--session", required=True, metavar="PATH",
                    help="session.jsonl 文件路径")
    up.add_argument("--dataset-name", default="caw-agent-eval-v1")
    up.add_argument("--item-id", required=True, help="对应的 dataset item ID")
    up.add_argument("--run-name", default=f"eval-run-{ts}",
                    help="Langfuse dataset run 名称")
    up.add_argument("--skill", default="cobo-agentic-wallet-sandbox",
                    help="Session skill 标签")

    args = parser.parse_args()

    if args.cmd == "list":
        cmd_list(
            dataset_name=args.dataset_name,
            item_id=args.item_id,
            fmt=args.format,
        )
    elif args.cmd == "upload":
        cmd_upload(
            session_path=args.session,
            dataset_name=args.dataset_name,
            item_id=args.item_id,
            run_name=args.run_name,
            skill=args.skill,
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
