"""
评测脚本公共工具模块。

提供 Langfuse 客户端初始化、数据集读取、session 上传和 dataset run 关联等功能。
供 run_eval_cc.py / run_eval_openclaw.py 等评测编排脚本共用。

环境变量:
    LANGFUSE_HOST         - Langfuse 服务地址
    LANGFUSE_PUBLIC_KEY   - Langfuse 公钥（数据集读写 + session 上传）
    LANGFUSE_SECRET_KEY   - Langfuse 私钥（数据集读写 + session 上传）
"""

import fcntl
import json
import os
import re
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langfuse import Langfuse

from upload_session import upload_session_file

# 自动加载 .env（不覆盖已设置的环境变量）
# 优先级：同目录 .env > ~/.caw-eval/.env（备用，skill sync 不会清除）
load_dotenv(Path(__file__).parent / ".env", override=False)
load_dotenv(Path.home() / ".caw-eval" / ".env", override=False)

_DEFAULT_HOST = "https://langfuse.1cobo.com"


# ── eval-mode / recipe-source 命名规范 ───────────────────────────────────────
# 新 canonical 值（2026-04 起）:
#   eval-mode:     e2e (全流程)        / pact (仅评 pact 构造) / onboard (onboarding 评估)
#   recipe-source: real (调真实 backend) / seed (注入 dataset recipe) / empty (注入空 recipe，对照组)
# 老值兼容映射（CLI/旧 manifest 仍可读取，内部规范化为新值）:
_EVAL_MODE_ALIAS = {"standard": "e2e", "recipe": "pact"}
_RECIPE_SOURCE_ALIAS = {
    "cc_with_recipe": "seed",
    "openclaw": "seed",
    "cc_no_recipe": "empty",
    "cc_real_recipe": "real",
    "oc_real_recipe": "real",
}


def _normalize_eval_mode(v: str) -> str:
    """老 eval-mode 值 → 新 canonical。空值返回 e2e（默认）。"""
    if not v:
        return "e2e"
    return _EVAL_MODE_ALIAS.get(v, v)


def _normalize_recipe_source(new_source: str, legacy_recipe_mode: str = "") -> str:
    """优先用新 --recipe-source；空值时回退到老 --recipe-mode 兼容映射。"""
    if new_source:
        return new_source
    if legacy_recipe_mode:
        return _RECIPE_SOURCE_ALIAS.get(legacy_recipe_mode, legacy_recipe_mode)
    return ""


def get_langfuse_client() -> Langfuse:
    """创建并返回 Langfuse 客户端实例。

    凭据优先级: LANGFUSE_DATASET_* → LANGFUSE_* → .env file.
    """

    def _pick(specific: str, generic: str) -> str:
        return os.environ.get(specific) or os.environ.get(generic) or ""

    pub = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    sec = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")
    if not pub or not sec:
        print(
            "[WARN] Langfuse credentials not set. "
            "Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY "
            "(or LANGFUSE_DATASET_PUBLIC_KEY + LANGFUSE_DATASET_SECRET_KEY) in .env or env vars."
        )
    host = _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST") or _DEFAULT_HOST

    return Langfuse(
        public_key=pub,
        secret_key=sec,
        host=host,
        timeout=120,
    )


_DATASET_URL_RE = re.compile(r"/datasets/([A-Za-z0-9_-]+)")


def resolve_dataset(arg: str, lf: Langfuse | None = None) -> str:
    """把 name / id / Langfuse URL 解析成规范的 dataset name。

    支持三种形式：
      - "caw-recipe-eval-v1"               （name，直接返回）
      - "cmoe5412o02asnb074juhtz15"        （Langfuse dataset cuid）
      - "https://langfuse.../datasets/<id>" （URL，抽取末段为 id）

    解析顺序：
      1. URL 形式 → 抽取 id 落到 id 流程
      2. 直接当 name `lf.api.datasets.get(arg)` —— 命中即返回
      3. 兜底：`lf.api.datasets.list` 翻页找 id 或 name 完全匹配

    找不到时抛 ValueError（调用方决定 abort 还是 fallback）。
    """
    if not arg:
        raise ValueError("dataset arg is empty")

    if arg.startswith("http://") or arg.startswith("https://"):
        m = _DATASET_URL_RE.search(arg)
        if not m:
            raise ValueError(f"cannot extract dataset id from URL: {arg}")
        arg = m.group(1)

    if lf is None:
        lf = get_langfuse_client()

    # 先按 name 直查（一次 API 调用）
    try:
        ds = lf.api.datasets.get(arg)
        return ds.name
    except Exception:
        pass

    # 兜底：分页 list 找 id/name 匹配
    page = 1
    while True:
        try:
            res = lf.api.datasets.list(page=page, limit=50)
        except Exception as e:
            raise ValueError(f"failed to list datasets while resolving {arg!r}: {e}") from e
        for d in res.data:
            if d.id == arg or d.name == arg:
                return d.name
        if len(res.data) < 50:
            break
        page += 1

    raise ValueError(f"dataset not found by name or id: {arg!r}")


def print_dataset_summary(dataset_name: str, lf: Langfuse | None = None) -> None:
    """打印一行数据集摘要，供 dispatch 启动时人眼复核。

    格式：dataset: <name> (id=<id>, items=<N>, chains={<sorted-comma-joined>})

    抓不到时打 [WARN] 但不 raise，避免阻塞主流程。
    """
    if lf is None:
        lf = get_langfuse_client()
    try:
        ds = lf.get_dataset(dataset_name)
        all_items = list(ds.items)
        # 只统计 ACTIVE 的（与 get_dataset_items 默认行为一致），ARCHIVED 单列计数
        active_items = [
            it for it in all_items if not str(getattr(it, "status", "") or "").endswith("ARCHIVED")
        ]
        archived_n = len(all_items) - len(active_items)
        chains: set[str] = set()
        for it in active_items:
            md = it.metadata if isinstance(it.metadata, dict) else {}
            ch = md.get("chain") or md.get("chain_id")
            if isinstance(ch, str) and ch:
                chains.add(ch)
        chains_str = ",".join(sorted(chains)) if chains else "?"
        ds_id = ""
        try:
            ds_id = lf.api.datasets.get(dataset_name).id or ""
        except Exception:
            pass
        archive_note = f", archived={archived_n}" if archived_n else ""
        print(
            f"dataset: {dataset_name} (id={ds_id or '?'}, items={len(active_items)}{archive_note}, chains={{{chains_str}}})",
            flush=True,
        )
    except Exception as e:
        print(f"[WARN] print_dataset_summary({dataset_name!r}) failed: {e}", flush=True)


def get_dataset_items(dataset_name: str, include_archived: bool = False) -> list[dict]:
    """从 Langfuse 拉取 dataset items。

    处理 input 为 str 或 dict 两种情况，返回标准化的 item 列表。
    `dataset_name` 也接受 id / URL（透明 resolve 到 name），便于一处加 id 全栈生效。

    默认 **过滤掉 status=ARCHIVED** 的 item —— Langfuse SDK `dataset.items` 同时
    返回 ACTIVE + ARCHIVED，但 ARCHIVED 通常是人工标记"不再有效"的废测试，跑了浪费
    资金（base 主网真金）+ 污染评分。如需访问归档项请显式 `include_archived=True`。
    """
    lf = get_langfuse_client()
    dataset_name = resolve_dataset(dataset_name, lf=lf)
    dataset = lf.get_dataset(dataset_name)
    items = sorted(dataset.items, key=lambda i: i.id)
    result = []
    archived_skipped = 0
    for item in items:
        # 过滤 ARCHIVED（默认行为）：item.status 是 DatasetStatus enum，
        # str() 后形如 "DatasetStatus.ARCHIVED"，用 endswith 匹配兼容老 SDK
        if not include_archived:
            status_str = str(getattr(item, "status", "") or "")
            if status_str.endswith("ARCHIVED"):
                archived_skipped += 1
                continue
        # input 可能是 str 或 dict
        inp = item.input if isinstance(item.input, dict) else {"user_message": item.input or ""}
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        exp = item.expected_output if isinstance(item.expected_output, dict) else {}
        # 优先用 metadata.id（如 E2E-01L1），回退到 Langfuse UUID
        item_id = meta.get("id", item.id)
        result.append(
            {
                "id": item_id,
                "langfuse_id": item.id,
                "user_message": inp.get("user_message", str(item.input or "")),
                "operation_type": meta.get("operation_type", ""),
                "difficulty": meta.get("difficulty", ""),
                "chain": meta.get("chain", ""),
                "success_criteria": exp.get("success_criteria", ""),
                "recipe": meta.get("recipe", ""),
            }
        )
    if archived_skipped:
        print(
            f"[INFO] dataset '{dataset_name}': skipped {archived_skipped} ARCHIVED item(s) "
            f"(use include_archived=True to include)"
        )
    return result


def upload_session(
    session_path: str,
    skill_name: str = "cobo-agentic-wallet-sandbox",
    trace_id: str = "",
    extra_metadata: dict | None = None,
) -> str | None:
    """上传单个 session.jsonl 到 Langfuse，返回实际 trace_id，失败返回 None。

    Args:
        trace_id: 外部指定的 trace ID（UUID）。为空时使用 session 文件内的 session_id。
        extra_metadata: 额外上下文（item_id、user_message 等），写入 trace metadata。
    """
    try:
        return upload_session_file(
            session_path,
            skill_name=skill_name,
            trace_id=trace_id,
            extra_metadata=extra_metadata,
        )
    except Exception as e:
        print(f"    [UPLOAD ERROR] {e}")
        return None


def link_to_dataset_run(
    lf: Langfuse,
    dataset_item_id: str,
    run_name: str,
    trace_id: str,
    run_description: str = "",
) -> None:
    """将 Langfuse trace 关联到 dataset item run。

    Args:
        dataset_item_id: Langfuse dataset item 的 UUID（不是 metadata id）。
        run_description: 可选的 run 描述，写入 Langfuse dataset run。
    """
    try:
        kwargs: dict = {
            "run_name": run_name,
            "dataset_item_id": dataset_item_id,
            "trace_id": trace_id,
        }
        if run_description:
            kwargs["run_description"] = run_description
        lf.api.dataset_run_items.create(**kwargs)
        print(f"    [LINKED] trace={trace_id[:8]}... -> run={run_name}")
    except Exception as e:
        print(f"    [LINK ERROR] {e}")


def batch_upload_sessions(
    run_dir: Path,
    run_name: str,
    dataset_name: str,
    skill: str = "cobo-agentic-wallet-sandbox",
    item_ids: list[str] | None = None,
    run_description: str = "",
    skip_link: bool = False,
    item_context_override: dict[str, dict] | None = None,
) -> dict[str, str]:
    """批量上传 session 到 Langfuse 并（可选）关联 dataset run。

    为每个 session 生成独立 trace UUID，上传后写 trace_map.json。
    返回 trace_map（item_id → trace UUID）。

    Args:
        run_description: 写入 Langfuse dataset run 的描述，建议包含 model/dataset/env 等信息。
        skip_link: True 时跳过 dataset_run_items 关联（trace 仍上传），适合调试少量 case 时
                   不污染 dataset run 列表。
        item_context_override: 直接提供 item 上下文（GTM inline 模式），跳过 get_dataset_items 调用。
                               格式：{item_id: {item_id, user_message, operation_type, difficulty}}
    """
    session_files = sorted(run_dir.glob("E2E-*.jsonl"))
    if not session_files:
        # 也支持非 E2E- 前缀的 session 文件（GTM inline 模式 item_id 可能是自定义格式）
        session_files = sorted(run_dir.glob("*.jsonl"))
    if item_ids:
        session_files = [f for f in session_files if f.stem in item_ids]

    if not session_files:
        print("[ERROR] 没有找到 session 文件")
        return {}

    lf = get_langfuse_client()

    if item_context_override is not None:
        # GTM inline 模式：item 不在 Langfuse dataset，跳过 get_dataset_items
        meta_to_langfuse: dict[str, str] = {}
        item_context: dict[str, dict] = item_context_override
    else:
        # 建立 metadata_id (E2E-01L1) → langfuse dataset item UUID 映射
        ds_items = get_dataset_items(dataset_name)
        meta_to_langfuse = {item["id"]: item["langfuse_id"] for item in ds_items}

        # item 上下文，写入 trace metadata（不写入 input，input 只放 session 级信息）
        item_context = {
            item["id"]: {
                "item_id": item["id"],
                "user_message": item.get("user_message", ""),
                "operation_type": item.get("operation_type", ""),
                "difficulty": item.get("difficulty", ""),
            }
            for item in ds_items
        }

    trace_map: dict[str, str] = {}

    print(f"=== 上传 {len(session_files)} 个 session (run: {run_name}) ===\n")

    for session_file in session_files:
        item_id = session_file.stem
        trace_id = str(uuid.uuid4())
        print(f"  [{item_id}] uploading... (trace_id={trace_id[:8]}...)")

        result_trace_id = upload_session(
            str(session_file),
            skill,
            trace_id=trace_id,
            extra_metadata=item_context.get(item_id),
        )
        if result_trace_id:
            trace_map[item_id] = result_trace_id
            print(f"    [INFO] trace_id: {result_trace_id}")
            if skip_link:
                print("    [SKIP LINK] --no-link: trace 已上传，未关联 dataset run")
            else:
                langfuse_item_id = meta_to_langfuse.get(item_id)
                if langfuse_item_id:
                    link_to_dataset_run(
                        lf, langfuse_item_id, run_name, result_trace_id, run_description
                    )
                else:
                    print(f"    [WARN] Dataset item not found for {item_id}, skipping link")
        else:
            print(f"    [ERROR] Upload failed for {item_id}")

    lf.flush()

    # 写入 trace_map.json，供 score 阶段使用。
    # 用 fcntl.flock 序列化读改写：streaming 模式下多 dispatch worker 会并发调本函数（item_ids=[id]），
    # 必须 merge 而非覆盖，否则后写者会丢前面 item 的 trace_id。
    trace_map_path = run_dir / "trace_map.json"
    trace_map_path.touch(exist_ok=True)
    with trace_map_path.open("r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            content = f.read().strip()
            existing = json.loads(content) if content else {}
            merged = {**existing, **trace_map}
            f.seek(0)
            f.truncate()
            f.write(json.dumps(merged, indent=2, ensure_ascii=False))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    print(f"\ntrace_map: {trace_map_path} ({len(merged)} items)")
    print("上传完成")

    return merged
