"""
评测运行时合规自检。

扫描评测脚本、数据集、prompt 模板、session 来源，拦截会污染评测结果的"禁用模式"：
- Prompt 里拼接 recipe / 禁 search / 硬编码"跳过预览"等评测约束（污染 agent 行为）
- 运行时用 Claude Code Task() 套娃 spawn subagent（偏离真实用户链路）
- Dataset item schema 违规
- Session cwd 不在服务器（本地调试 session 不能作为正式评测）

用法：
    python runtime_compliance.py --check-all
    python runtime_compliance.py --check-prompts
    python runtime_compliance.py --check-runtime
    python runtime_compliance.py --check-session-source <RUN_DIR> --strict
"""

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent
_CAW_EVAL_DIR = _SCRIPTS_DIR.parent

# ── 禁用模式清单 ─────────────────────────────────────────────────────────────

# Prompt 模板里的禁用词（应该不出现在 build_eval_prompt 的输出或 item 内容里）
BANNED_PROMPT_PATTERNS: list[tuple[str, str]] = [
    (
        r"CAW_RECIPE_FILE=\S+\s+caw\s+recipe\s+search",
        "prompt 里要求 agent 加 env 前缀调 recipe search（应通过进程 env 注入）",
    ),
    (
        r"禁止(?:使用|调用)\s*caw\s+recipe\s+search",
        "prompt 里禁止 search（不真实，真实用户会 search）",
    ),
    (
        r"跳过(?:展示)?预览(?:和|并|、)?等待用户确认",
        "prompt 硬编码'跳过预览'评测约束（应换成'用户预先授权'语气或 metadata 配置）",
    ),
]

# 运行时代码里的禁用模式
BANNED_RUNTIME_PATTERNS: list[tuple[str, str, list[str]]] = [
    (
        r"Task\s*\(",
        "run_eval_*.py 使用 Claude Code 的 Task() 工具 spawn subagent 跑评测（应用 headless claude CLI）",
        ["run_eval_cc.py", "run_eval_openclaw.py"],
    ),
]


def _scan_file(path: Path, patterns: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """扫描文件按行检测 banned pattern，返回 (lineno, line, reason)。"""
    hits: list[tuple[int, str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return hits
    for i, line in enumerate(lines, start=1):
        for pattern, reason in patterns:
            if re.search(pattern, line):
                hits.append((i, line.strip(), reason))
    return hits


def check_prompts(strict: bool = False) -> bool:
    """扫描 build_eval_prompt 相关文件的 prompt 模板，检查禁用词。"""
    print("[runtime_compliance] check_prompts")
    targets = [
        _SCRIPTS_DIR / "run_eval_cc.py",
        _SCRIPTS_DIR / "run_eval_openclaw.py",
    ]
    any_violation = False
    for target in targets:
        if not target.exists():
            continue
        hits = _scan_file(target, BANNED_PROMPT_PATTERNS)
        if hits:
            any_violation = True
            print(f"  [FAIL] {target.name}:")
            for ln, line, reason in hits:
                print(f"    L{ln}: {line[:100]}")
                print(f"          理由: {reason}")
        else:
            print(f"  [OK] {target.name}")
    return not any_violation


def check_runtime(strict: bool = False) -> bool:
    """扫描运行时代码模式（subagent 套娃等）。"""
    print("[runtime_compliance] check_runtime")
    any_violation = False
    for pattern, reason, filenames in BANNED_RUNTIME_PATTERNS:
        for fname in filenames:
            target = _SCRIPTS_DIR / fname
            if not target.exists():
                continue
            hits = _scan_file(target, [(pattern, reason)])
            if hits:
                any_violation = True
                print(f"  [FAIL] {target.name}:")
                for ln, line, _ in hits:
                    print(f"    L{ln}: {line[:100]}")
                    print(f"          理由: {reason}")
            else:
                print(f"  [OK] {target.name} ({pattern})")
    return not any_violation


def check_dataset_schema(strict: bool = False) -> bool:
    """调 validate_dataset.py 跑本地 schema 校验。"""
    print("[runtime_compliance] check_dataset_schema")
    try:
        from schemas import validate_item
        from generate_dataset import DATASET_ITEMS
    except ImportError as e:
        print(f"  [SKIP] 无法加载 schemas / generate_dataset: {e}")
        return True

    passed, failed = 0, 0
    for it in DATASET_ITEMS:
        md = dict(it["metadata"])
        md.setdefault("id", it["id"])
        full = {**it, "metadata": md}
        try:
            validate_item(full)
            passed += 1
        except Exception:
            failed += 1
    print(f"  [RESULT] PASS={passed} FAIL={failed}")
    return failed == 0


def check_session_source(run_dir: str, strict: bool = False) -> bool:
    """扫描指定 run_dir 下所有 session .jsonl，检查是否为服务器来源。

    正式评测 session 必须是 server 来源（cwd=/home/ubuntu）——本地环境的
    skill/caw/context 和服务器漂移，实测 E2E 差 0.18，不能代表真实用户。
    本地来源 session 会标 FAIL，strict 模式会阻止 run 被作为正式结果。
    """
    from pathlib import Path as _Path

    print(f"[runtime_compliance] check_session_source run_dir={run_dir}")
    run_path = _Path(run_dir).expanduser()
    if not run_path.is_dir():
        print(f"  [SKIP] {run_dir} 不是目录")
        return True

    try:
        from score_traces import _detect_session_source, _parse_session_file
    except ImportError:
        print("  [SKIP] 无法加载 score_traces（schemas/env 问题）")
        return True

    any_non_server = False
    for sess_file in sorted(run_path.glob("*.jsonl")):
        if sess_file.name == "manifest.json":
            continue
        try:
            session = _parse_session_file(str(sess_file))
            src = _detect_session_source(session)
        except Exception as e:
            print(f"  [ERROR] {sess_file.name}: {e}")
            any_non_server = True
            continue
        marker = "OK" if src == "server" else "FAIL"
        cwd = session.get("cwd", "(none)") or "(none)"
        print(f"  [{marker}] {sess_file.name}: source={src} cwd={cwd[:60]}")
        if src != "server":
            any_non_server = True

    return not any_non_server


def assert_pre_run() -> None:
    """在 prepare/dispatch 入口调用：扫描 prompt / runtime 禁用模式，违规 exit 1。

    是 pre-commit 下放到运行时的守卫——替代 CI 拦截本地绕过规则的情况。
    """
    ok_prompts = check_prompts()
    ok_runtime = check_runtime()
    if not (ok_prompts and ok_runtime):
        print("\n[ERROR] runtime_compliance 自检失败，拒绝执行评测", file=sys.stderr)
        print("       详情见上方 [FAIL] 行", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-all", action="store_true")
    parser.add_argument("--check-prompts", action="store_true")
    parser.add_argument("--check-runtime", action="store_true")
    parser.add_argument("--check-schema", action="store_true")
    parser.add_argument(
        "--check-session-source",
        metavar="RUN_DIR",
        help="扫描 RUN_DIR 下的 session.jsonl，验证是否为服务器来源（正式评测前置）",
    )
    parser.add_argument("--strict", action="store_true", help="任一违规 exit 1")
    args = parser.parse_args()

    if args.check_all:
        args.check_prompts = args.check_runtime = args.check_schema = True

    if not any(
        [args.check_prompts, args.check_runtime, args.check_schema, args.check_session_source]
    ):
        parser.error(
            "必须指定 --check-prompts / --check-runtime / --check-schema / "
            "--check-session-source <RUN_DIR> / --check-all"
        )

    overall_ok = True
    if args.check_prompts:
        overall_ok &= check_prompts(args.strict)
    if args.check_runtime:
        overall_ok &= check_runtime(args.strict)
    if args.check_schema:
        overall_ok &= check_dataset_schema(args.strict)
    if args.check_session_source:
        overall_ok &= check_session_source(args.check_session_source, args.strict)

    print()
    print("=== runtime_compliance: {} ===".format("PASS" if overall_ok else "FAIL"))
    return 0 if overall_ok or not args.strict else 1


if __name__ == "__main__":
    sys.exit(main())
