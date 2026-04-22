#!/usr/bin/env python3
"""
Claude Code 评测编排脚本 — 从本地 Mac dispatch 到服务器跑 headless claude 评测。

子命令:
  dispatch         — 本地 Mac 端：并行调度 N 台服务器跑评测（动态队列）
  run              — 服务器端：headless 逐 item 执行（通常由 dispatch 调用）
  upload           — 批量上传 session 到 Langfuse 并关联 dataset run
  score            — 对 run 的 session 评分（调 score_traces.py）
  metrics          — 从 session 提取运行指标
  import-sessions  — 从外部目录导入 session 文件

用法:
    # 服务器 dispatch（正式评测路径）
    python run_eval_cc.py dispatch --run-name eval-cc-sonnet-20260411 \\
      --server <name:zone:project> [--eval-mode recipe --recipe-mode cc_with_recipe]

    # 上传 + 评分
    python run_eval_cc.py upload  --run-name eval-cc-sonnet-20260411
    python run_eval_cc.py score   --run-name eval-cc-sonnet-20260411 --report
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eval_utils import batch_upload_sessions, get_dataset_items

_SCRIPTS_DIR = Path(__file__).parent

# Headless CC session 存储根（claude CLI 按 cwd 写入 <cwd-sanitized>/<uuid>.jsonl）
_CC_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 评测 run 的本地存储目录
_RUNS_DIR = Path.home() / ".caw-eval" / "runs"

# Recipe 评测：每 item 写一份 recipe JSON 到此目录，agent 通过 CAW_RECIPE_FILE 指向本 item 的文件
# 与 openclaw 共享同一归档结构：/tmp/caw-eval-recipes/{run_name}/{item_id}.json
RECIPE_ARCHIVE_ROOT = Path("/tmp/caw-eval-recipes")

_CAW_BIN = Path.home() / ".cobo-agentic-wallet" / "bin" / "caw"

# dispatch 子命令常量 — 服务器端 run 子命令超时（默认 10 分钟）
_DEFAULT_CC_TIMEOUT = 600

# run 子命令导出 session 的公共路径（服务器端用 644 权限，方便 dispatch scp 拉回）
_SESSION_EXPORT_DIR = Path("/tmp/caw-eval-cc-sessions")


def _gcloud_env() -> dict:
    """subprocess 调 gcloud 的 env：Mac 上 gcloud 禁用 py3.13 需要指向 py3.11。

    用户可通过 CLOUDSDK_PYTHON 环境变量覆盖；未设时自动探测 Homebrew py3.11。
    """
    env = os.environ.copy()
    if env.get("CLOUDSDK_PYTHON"):
        return env
    for candidate in (
        "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3.11",
        shutil.which("python3.11") or "",
    ):
        if candidate and Path(candidate).exists():
            env["CLOUDSDK_PYTHON"] = candidate
            break
    return env


def _write_recipe_archive(run_name: str, item_id: str, recipe_content: str) -> Path:
    """写一份 recipe JSON 到 /tmp/caw-eval-recipes/{run_name}/{item_id}.json，
    caw recipe search 通过 CAW_RECIPE_FILE 指向该路径即可读取本地内容。

    - cc_with_recipe 模式：recipe_content 为测试目标 recipe（agent search 返回它）

    L1: 写入后立即 roundtrip 读回 —— 防止 json 序列化、文件系统、编码环节任何一步丢字节
    """
    recipes_json = {
        "message": "",
        "result": {
            "data": {
                "count": 1,
                "results": [{"content": recipe_content}],
            }
        },
    }
    archive_file = RECIPE_ARCHIVE_ROOT / run_name / f"{item_id}.json"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text(
        json.dumps(recipes_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # L1 roundtrip 验证
    try:
        roundtrip = json.loads(archive_file.read_text(encoding="utf-8"))
        written = roundtrip["result"]["data"]["results"][0]["content"]
    except (OSError, json.JSONDecodeError, KeyError, IndexError) as e:
        raise RuntimeError(
            f"_write_recipe_archive roundtrip parse failed for {item_id}: {e}"
        ) from e
    if written != recipe_content:
        raise RuntimeError(
            f"_write_recipe_archive content mismatch for {item_id} "
            f"(input={len(recipe_content)}B, written={len(written)}B)"
        )
    return archive_file


def _write_empty_recipe_archive(run_name: str, item_id: str) -> Path:
    """cc_no_recipe 模式：写 count=0 的空 recipe JSON。

    cc_no_recipe 是对照组——agent 仍然按真实用户流程自主触发 `caw recipe search`，
    但 search 返回空结果。这样 with_recipe vs no_recipe 的分数差 = recipe 提供的价值。

    写入后立即 roundtrip 验证 count=0 + results 为空。
    """
    empty_json = {
        "message": "",
        "result": {
            "data": {
                "count": 0,
                "results": [],
            }
        },
    }
    archive_file = RECIPE_ARCHIVE_ROOT / run_name / f"{item_id}.json"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text(json.dumps(empty_json, ensure_ascii=False, indent=2), encoding="utf-8")
    # L1 roundtrip
    try:
        roundtrip = json.loads(archive_file.read_text(encoding="utf-8"))
        if roundtrip["result"]["data"]["count"] != 0 or roundtrip["result"]["data"]["results"]:
            raise RuntimeError(
                f"_write_empty_recipe_archive roundtrip mismatch for {item_id}: "
                f"expected count=0/empty, got {roundtrip['result']['data']}"
            )
    except (OSError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(
            f"_write_empty_recipe_archive roundtrip failed for {item_id}: {e}"
        ) from e
    return archive_file


def build_eval_prompt(
    item: dict,
    eval_mode: str = "standard",
    recipe_mode: str = "",
    run_name: str = "",
    target_env: str = "local",
) -> str:
    """构建单个 item 的评测 prompt。

    Args:
        eval_mode: "standard" 标准模式, "recipe" recipe 评测模式
        recipe_mode: "cc_with_recipe" / "cc_no_recipe" / "openclaw"（仅 recipe 模式有效）
        run_name: run 名，cc_with_recipe 模式下用于定位 per-item recipe 归档文件
        target_env: "local"（本地 Mac / VSCode，用 dev skill）或
                    "server"（headless 服务器跑评测，用 sandbox skill + 加 Environment 段）
    """
    item_id = item["id"]
    user_message = item["user_message"]
    recipe_content = item.get("recipe", "")

    skill_name = (
        "cobo-agentic-wallet-sandbox" if target_env == "server" else "cobo-agentic-wallet-dev"
    )
    env_section = (
        """
## Environment（环境已就绪，无需安装）

- `caw` CLI 已安装于 `~/.cobo-agentic-wallet/bin/caw`，**无需 npm install 或任何安装步骤**
- 如 `caw` 不在 PATH，执行：`export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"`
- Onboarding 已完成，钱包和网络已配置好，直接执行任务即可
"""
        if target_env == "server"
        else ""
    )

    # F3: 评测约束用"用户先前上下文"语气替代硬指令。
    # 未 paired 场景下，用户已在 CC 中与 agent 沟通过，预先授权跳过 preview——
    # 这是真实用户"信任 agent、授权自主执行"的场景，不是评测造假。
    # owner_linked=false 时自动 approve 本来就是 skill 的合规行为，skill 会自主做，不再 prompt 里重复提示。
    prompt = f"""[EVAL:{item_id}]
{skill_name} skill 已激活。
{env_section}
**用户先前上下文（任务前已与 agent 协商过）：** 用户**预先授权**对本次任务生成的 pact 无需再次展示 preview 预览和等待 "yes" 确认，直接 `caw pact submit` 即可视为已获得用户明确确认。用户希望你自主、连续地完成任务，不被交互打断（如遇 `owner_linked=true` 需要 Cobo Agentic Wallet app 审批的真实阻塞，记录原因并跳过）。
"""

    if eval_mode == "recipe":
        prompt += """
**交易构建模式（重要）：**
- 交易成功提交后（caw tx transfer/call/sign-message 返回 status=Initiated 或 PendingApproval），**立即停止**
- 不要轮询交易状态（不要 caw tx get）
- 不要等待链上确认
- 只需报告交易已成功提交（含 transaction_id/request_id），然后结束
"""
        # Recipe 注入改造（F2）：不再在 prompt 里告诉 agent 加 env 前缀；
        # 改由 _run_single_cc_task 启动 claude 前把 CAW_RECIPE_FILE 放进子进程 env，
        # agent 正常调 `caw recipe search`，caw 自动读本地文件，行为等价 openclaw 模式。
        #
        # cc_with_recipe vs cc_no_recipe（对照组设计）：两者都让 agent 按真实用户流程自主
        # 调 `caw recipe search`，只是返回内容不同：
        #   - cc_with_recipe: 返回指定的（要测试的）recipe 内容
        #   - cc_no_recipe:   返回空结果（count=0），对照组，看没 recipe 时 agent 表现
        # with 和 no 的分数差即 recipe 提供的价值。
        if recipe_mode == "cc_with_recipe" and recipe_content and run_name:
            _write_recipe_archive(run_name, item_id, recipe_content)
        elif recipe_mode == "cc_no_recipe" and run_name:
            _write_empty_recipe_archive(run_name, item_id)

    prompt += f"""
按照以下用户指令完成操作：

{user_message}"""

    return prompt


def _recipe_archive_path(run_name: str, item_id: str) -> Path:
    """计算 recipe 归档文件的绝对路径，供 _run_single_cc_task 设 CAW_RECIPE_FILE env 用。

    与 _write_recipe_archive 内部的命名必须保持一致。
    """
    return RECIPE_ARCHIVE_ROOT / run_name / f"{item_id}.json"


# ── prepare 子命令 ──────────────────────────────────────────────────────────────


# ── run 子命令（服务器端 headless 执行单个 item）─────────────────────────────


def _session_export_path(item_id: str) -> Path:
    """公共导出路径，dispatch 从此拉 session 回本地（644 权限）。"""
    return _SESSION_EXPORT_DIR / f"{item_id}.jsonl"


def _sanitize_cwd(cwd: Path) -> str:
    """claude headless 把 session 写入 ~/.claude/projects/<sanitized-cwd>/<uuid>.jsonl。"""
    return str(cwd.resolve()).replace("/", "-")


async def _revoke_active_pacts_async() -> None:
    """异步 revoke 所有 active pact；失败不阻塞评测。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_CAW_BIN),
            "pact",
            "list",
            "--status",
            "active",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return
        pacts = json.loads(stdout.decode()).get("result", {}).get("pacts", [])
        for p in pacts:
            pid = p.get("id", "")
            if not pid:
                continue
            rp = await asyncio.create_subprocess_exec(
                str(_CAW_BIN),
                "pact",
                "revoke",
                pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(rp.communicate(), timeout=10)
        if pacts:
            print(f"  revoked {len(pacts)} active pact(s)")
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError):
        pass


async def _run_single_cc_task(
    item: dict,
    run_dir: Path,
    timeout: int,
    eval_mode: str,
    recipe_mode: str,
    run_name: str,
    model: str,
) -> str:
    """服务器端：执行单个 item，收集 session 到 run_dir 和公共导出目录。

    流程：revoke pact → 生成 prompt → 分配固定 UUID → 调 claude headless →
    将产出的 jsonl 复制到 run_dir/{item_id}.jsonl + /tmp/caw-eval-cc-sessions/{item_id}.jsonl。
    返回 "ok" / "error:<reason>"。
    """
    item_id = item["id"]
    session_id = str(uuid.uuid4())
    cwd = Path.home()
    raw_session_path = _CC_PROJECTS_DIR / _sanitize_cwd(cwd) / f"{session_id}.jsonl"

    await _revoke_active_pacts_async()

    prompt = build_eval_prompt(
        item,
        eval_mode=eval_mode,
        recipe_mode=recipe_mode,
        run_name=run_name,
        target_env="server",
    )

    # F2: cc_with_recipe / cc_no_recipe 都把 CAW_RECIPE_FILE 注入子进程 env，
    # 让 caw recipe search 自动读本地文件，不再依赖 prompt 前缀。
    # 两种模式下 archive 文件内容不同（测试目标 recipe vs 空对照），
    # agent 行为一致：都按真实用户流程自主 search。
    child_env = os.environ.copy()
    if recipe_mode in ("cc_with_recipe", "cc_no_recipe") and run_name:
        archive_file = _recipe_archive_path(run_name, item_id)
        if archive_file.exists():
            child_env["CAW_RECIPE_FILE"] = str(archive_file)

    print(f"STAGE: claude_start item={item_id} sid={session_id}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "-p",
        "--session-id",
        session_id,
        "--dangerously-skip-permissions",
        "--output-format",
        "text",
        "--model",
        model,
        prompt,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"  [{item_id}] TIMEOUT ({timeout}s)", flush=True)
        status = "error:timeout"
        rc = -1
        stdout = stderr = b""

    if rc == 0:
        tail = stdout.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        print("  agent output tail:")
        for line in tail:
            print(f"    {line}")
        status = "ok"
    elif rc == -1:
        pass
    else:
        err_tail = stderr.decode("utf-8", errors="replace").strip()[-400:]
        print(f"  [{item_id}] ERROR rc={rc} stderr_tail={err_tail}", flush=True)
        status = f"error:rc_{rc}"

    print(f"STAGE: claude_done status={status} item={item_id}", flush=True)

    if raw_session_path.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        dst = run_dir / f"{item_id}.jsonl"
        shutil.copy2(raw_session_path, dst)
        _SESSION_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        export_path = _session_export_path(item_id)
        shutil.copy2(raw_session_path, export_path)
        os.chmod(export_path, 0o644)
        size_kb = dst.stat().st_size / 1024
        print(f"  [{item_id}] session -> {dst.name} ({size_kb:.0f} KB)", flush=True)
        print(f"STAGE: session_collected item={item_id}", flush=True)
    else:
        print(f"  [{item_id}] no session file at {raw_session_path}", flush=True)
        if status == "ok":
            status = "error:no_session"

    return status


async def _cmd_run(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    timeout: int,
    model: str,
    eval_mode: str,
    recipe_mode: str,
    inline_item: str | None,
) -> None:
    """服务器端 run 子命令：逐 item headless 执行评测。

    两种 item 来源：
      - inline_item：dispatch 直接推 item JSON（推荐，服务器免配 Langfuse）
      - dataset_name + item_ids：从 Langfuse 拉（服务器需配 LANGFUSE_* 环境变量）
    """
    try:
        git_result = subprocess.run(
            ["git", "-C", str(_SCRIPTS_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        skill_ver = git_result.stdout.strip() if git_result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        skill_ver = "unknown"
    print(f"SKILL_VERSION={skill_ver}", flush=True)

    if inline_item is not None:
        items = [json.loads(inline_item)]
    else:
        items = get_dataset_items(dataset_name)
        if item_ids:
            items = [i for i in items if i["id"] in item_ids]

    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)

    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== CC 服务器端评测 (run: {run_name}) ===")
    print(f"数据集: {dataset_name} ({len(items)} items)")
    print(f"model: {model} timeout: {timeout}s / item")
    print(f"eval_mode: {eval_mode} recipe_mode: {recipe_mode or '-'}")
    print()

    results: dict[str, str] = {}
    for i, item in enumerate(items):
        iid = item["id"]
        op = item.get("operation_type", "")
        diff = item.get("difficulty", "")
        print(f"[{i + 1}/{len(items)}] {iid} ({op} {diff})")
        status = await _run_single_cc_task(
            item, run_dir, timeout, eval_mode, recipe_mode, run_name, model
        )
        results[iid] = status

    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "cc-headless",
        "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        "model": model,
        "eval_mode": eval_mode,
        "recipe_mode": recipe_mode,
        "items": {
            item["id"]: {
                "status": results.get(item["id"], "skipped"),
                "operation_type": item.get("operation_type", ""),
                "difficulty": item.get("difficulty", ""),
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    ok_count = sum(1 for s in results.values() if s == "ok")
    err_count = sum(1 for s in results.values() if s.startswith("error:"))
    print(f"\n=== 完成: {ok_count} ok / {err_count} error (共 {len(items)}) ===")
    print(f"文件位置: {run_dir}")
    if err_count > 0:
        failed = [iid for iid, s in results.items() if s.startswith("error:")]
        print(f"失败项: {', '.join(failed)}")


# ── upload 子命令 ──────────────────────────────────────────────────────────────


def cmd_upload(
    run_name: str,
    dataset_name: str,
    item_ids: list[str] | None,
    skill: str,
    model: str,
    model_full: str,
    description: str,
    skip_link: bool = False,
) -> None:
    """批量上传 run 目录下的 session 文件到 Langfuse。

    Args:
        skip_link: True 时只上传 trace，不创建/关联 dataset run（适合调试少量 case）。
    """
    run_dir = _RUNS_DIR / run_name

    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        print(f"请先运行: python run_eval_cc.py collect --run-name {run_name}")
        sys.exit(1)

    # 自动构建 run_description（如未手动指定）
    run_description = description
    if not run_description:
        n_sessions = len(list(run_dir.glob("E2E-*.jsonl")))
        display_model = model_full or model
        run_description = (
            f"Claude Code 评测 | model: {display_model} | dataset: {dataset_name}"
            f" ({n_sessions} cases) | env: Claude Code"
        )

    batch_upload_sessions(
        run_dir, run_name, dataset_name, skill, item_ids, run_description, skip_link=skip_link
    )


# ── score 子命令 ───────────────────────────────────────────────────────────────


def cmd_score(
    run_name: str,
    dataset_name: str,
    report: bool,
    dump_judge: str | None,
    judge_results: str | None,
) -> None:
    """对 run 目录下的 session 评分。"""
    run_dir = _RUNS_DIR / run_name

    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    # 构建 score_traces.py 调用参数
    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "score_traces.py"),
        "session",
        "--session",
        str(run_dir),
    ]

    if report:
        cmd.append("--report")
    if dump_judge:
        cmd.extend(["--dump-judge-requests", dump_judge])
    if judge_results:
        cmd.extend(["--judge-results", judge_results])

    print(f"=== 评分 (run: {run_name}) ===\n")
    result = subprocess.run(cmd, timeout=600)
    sys.exit(result.returncode)


# ── import-sessions 子命令 ────────────────────────────────────────────────────


def cmd_import_sessions(
    from_dir: str,
    run_name: str,
) -> None:
    """从外部目录导入 session 文件到本地 run 目录。用于导入 Openclaw 服务器拉下来的 session。"""
    src_dir = Path(from_dir)
    if not src_dir.exists():
        print(f"[ERROR] 源目录不存在: {src_dir}")
        sys.exit(1)

    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    session_files = list(src_dir.glob("E2E-*.jsonl"))
    if not session_files:
        # 也尝试不带 E2E 前缀的 jsonl 文件
        session_files = list(src_dir.glob("*.jsonl"))

    if not session_files:
        print(f"[ERROR] 源目录中没有 session 文件: {src_dir}")
        sys.exit(1)

    print(f"=== 导入 {len(session_files)} 个 session 到 {run_name} ===\n")

    imported = 0
    for sf in sorted(session_files):
        dst = run_dir / sf.name
        shutil.copy2(sf, dst)
        size_kb = dst.stat().st_size / 1024
        print(f"  [{sf.stem}] OK  ({size_kb:.0f} KB)")
        imported += 1

    # 复制 manifest（如果有）
    manifest_src = src_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, run_dir / "manifest.json")

    print(f"\n导入完成: {imported} 个 session")
    print(f"文件位置: {run_dir}")
    print("\n下一步：")
    print(f"  python run_eval_cc.py score --run-name {run_name} --report")


# ── dispatch 子命令（本地 Mac 并行调度 N 台服务器，动态队列）────────────────────


_BUSY_PROBE_CMD = (
    "cc=$(pgrep -af 'claude -p' 2>/dev/null | grep -v grep | head -1); "
    "oc=$(pgrep -af 'openclaw agent --agent eval-' 2>/dev/null | grep -v grep | head -1); "
    'echo "cc=$cc"; echo "oc=$oc"'
)

_REMOTE_SCRIPTS_DIR = "/home/ubuntu/.agents/skills/caw-eval/scripts"


def _parse_server_spec(spec: str) -> dict:
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid server spec '{spec}', expected 'name:zone:project'")
    return {"name": parts[0], "zone": parts[1], "project": parts[2]}


async def _ssh_exec_ubuntu(srv: dict, remote_cmd: str) -> tuple[int, str, str]:
    """SSH 到 server 以 ubuntu 用户执行命令，返回 (rc, stdout, stderr)。"""
    ssh_cmd = [
        "gcloud",
        "compute",
        "ssh",
        "--zone",
        srv["zone"],
        srv["name"],
        "--tunnel-through-iap",
        "--project",
        srv["project"],
        "--ssh-flag=-o ServerAliveInterval=60",
        "--ssh-flag=-o ServerAliveCountMax=10",
        "--",
        f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_gcloud_env(),
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _scp_pull_file(srv: dict, remote_path: str, local_path: Path) -> bool:
    """从 server 拉文件到本地（remote 需 644 可读权限）。"""
    scp_cmd = [
        "gcloud",
        "compute",
        "scp",
        "--zone",
        srv["zone"],
        "--tunnel-through-iap",
        "--project",
        srv["project"],
        f"{srv['name']}:{remote_path}",
        str(local_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *scp_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_gcloud_env(),
    )
    _, stderr = await proc.communicate()
    if (proc.returncode or 0) != 0:
        print(f"  scp failed: {stderr.decode('utf-8', 'replace').strip()[:200]}")
        return False
    return local_path.exists()


async def _check_server_busy(srv: dict) -> str:
    """返回 'free' / 'cc' / 'openclaw' / 'error'。"""
    rc, stdout, _ = await _ssh_exec_ubuntu(srv, _BUSY_PROBE_CMD)
    if rc != 0:
        return "error"
    cc_line = ""
    oc_line = ""
    for line in stdout.splitlines():
        if line.startswith("cc=") and line[3:].strip():
            cc_line = line[3:].strip()
        elif line.startswith("oc=") and line[3:].strip():
            oc_line = line[3:].strip()
    if cc_line:
        return "cc"
    if oc_line:
        return "openclaw"
    return "free"


def _build_remote_run_cmd(
    item_inline: str,
    run_name: str,
    timeout: int,
    model: str,
    eval_mode: str,
    recipe_mode: str,
) -> str:
    """拼接远端 shell 命令：source env + export PATH + python 执行 run 子命令。"""
    core = (
        f"source ~/.claude_code.env; "
        f"export PATH=/home/ubuntu/.cobo-agentic-wallet/bin:"
        f"/home/ubuntu/.npm-global/bin:$PATH; "
        f"cd ~ && "
        f"python3 -u {_REMOTE_SCRIPTS_DIR}/run_eval_cc.py run "
        f"--run-name {shlex.quote(run_name)} "
        f"--inline-item {shlex.quote(item_inline)} "
        f"--timeout {timeout} "
        f"--model {shlex.quote(model)}"
    )
    if eval_mode and eval_mode != "standard":
        core += f" --eval-mode {shlex.quote(eval_mode)}"
    if recipe_mode:
        core += f" --recipe-mode {shlex.quote(recipe_mode)}"
    return core


async def _dispatch_worker_cc(
    srv: dict,
    queue: asyncio.Queue,
    item_map: dict[str, dict],
    item_results: dict[str, tuple[str, str]],
    run_name: str,
    timeout: int,
    model: str,
    eval_mode: str,
    recipe_mode: str,
    log_dir: Path,
    local_run_dir: Path,
) -> str:
    """动态 worker：持续从队列取 item 执行。

    每个 item 做：远端 ssh 跑 run 子命令 → 拉 session jsonl 回本地 run_dir。
    item_results[item_id] = (server_name, status)。
    """
    while True:
        try:
            item_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        item = item_map[item_id]
        item_inline = json.dumps(item, ensure_ascii=False)
        remote_cmd = _build_remote_run_cmd(
            item_inline, run_name, timeout, model, eval_mode, recipe_mode
        )
        log_file = log_dir / f"{srv['name']}-{item_id}.log"
        print(f"[DISPATCH→ {srv['name']}] item={item_id}")

        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"# item={item_id}\n# server={srv['name']}\n\n")
            f.flush()
            ssh_cmd = [
                "gcloud",
                "compute",
                "ssh",
                "--zone",
                srv["zone"],
                srv["name"],
                "--tunnel-through-iap",
                "--project",
                srv["project"],
                "--ssh-flag=-o ServerAliveInterval=60",
                "--ssh-flag=-o ServerAliveCountMax=10",
                "--",
                f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
            ]
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=f,
                stderr=asyncio.subprocess.STDOUT,
                env=_gcloud_env(),
            )
            rc = await proc.wait()

        status = "ok" if rc == 0 else f"ssh_rc_{rc}"
        if rc == 0:
            remote_session = f"/tmp/caw-eval-cc-sessions/{item_id}.jsonl"
            local_session = local_run_dir / f"{item_id}.jsonl"
            local_run_dir.mkdir(parents=True, exist_ok=True)
            pulled = await _scp_pull_file(srv, remote_session, local_session)
            if not pulled:
                status = "no_session"
        print(f"[DISPATCH← {srv['name']}] item={item_id} {status}")
        item_results[item_id] = (srv["name"], status)
        queue.task_done()
    return srv["name"]


async def _sync_scripts_to_server(srv: dict, scripts_src: Path) -> bool:
    """rsync 本地 scripts/ 到服务器 ~/.agents/skills/caw-eval/scripts/。"""
    nonce = uuid.uuid4().hex[:8]
    tmp_remote = f"/tmp/caw-eval-scripts-{nonce}.tar.gz"
    # 打包本地 scripts 目录（排除 .pyc / __pycache__）
    archive = Path("/tmp") / f"caw-eval-scripts-{nonce}.tar.gz"
    subprocess.run(
        [
            "tar",
            "czf",
            str(archive),
            "-C",
            str(scripts_src.parent),
            "--exclude=__pycache__",
            "--exclude=*.pyc",
            scripts_src.name,
        ],
        check=True,
    )
    try:
        scp_cmd = [
            "gcloud",
            "compute",
            "scp",
            "--zone",
            srv["zone"],
            "--tunnel-through-iap",
            "--project",
            srv["project"],
            str(archive),
            f"{srv['name']}:{tmp_remote}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *scp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_gcloud_env(),
        )
        _, stderr = await proc.communicate()
        if (proc.returncode or 0) != 0:
            print(f"  {srv['name']}: scp 失败 {stderr.decode()[:200]}")
            return False
    finally:
        archive.unlink(missing_ok=True)

    # /tmp sticky bit: ubuntu 无法 rm luochong_cobo_com 拥有的文件，用 sudo rm 强删；
    # sudo chown/rm 失败不致命（目录/文件权限不一致时走 || true 继续）
    extract_cmd = (
        f"mkdir -p ~/.agents/skills/caw-eval && "
        f"(sudo chown -R ubuntu:ubuntu ~/.agents/skills/caw-eval 2>/dev/null || true) && "
        f"tar xzf {tmp_remote} -C ~/.agents/skills/caw-eval/ && "
        f"(sudo rm -f {tmp_remote} 2>/dev/null || true) && echo sync-done"
    )
    rc, stdout, stderr = await _ssh_exec_ubuntu(srv, extract_cmd)
    ok = rc == 0 and "sync-done" in stdout
    if not ok:
        tail = (stderr or stdout)[-200:]
        print(f"  {srv['name']}: 解压失败 rc={rc} err={tail!r}")
    return ok


# Content hash 算法（本地 + 服务器都用相同命令，保证跨平台一致）
# 对 tar stream 算 hash 在 bsdtar / gnu tar 之间不兼容，所以用 find+sort+shasum 的逐文件聚合方式
_DIR_CONTENT_HASH_CMD = (
    "cd {path} && find . -type f "
    "! -name '*.pyc' ! -name '.DS_Store' ! -path './__pycache__/*' "
    "-print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null "
    "| shasum -a 256 | awk '{{print $1}}'"
)
_FILE_SHA256_CMD = "shasum -a 256 {path} 2>/dev/null | awk '{{print $1}}'"


def _local_content_hash(path: Path, is_file: bool) -> str:
    """本地算目录或文件的内容 hash。和 _remote_content_hash 用相同算法。"""
    if not path.exists():
        return "missing"
    import shlex as _shlex

    cmd = (
        _FILE_SHA256_CMD.format(path=_shlex.quote(str(path)))
        if is_file
        else _DIR_CONTENT_HASH_CMD.format(path=_shlex.quote(str(path)))
    )
    r = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.stdout.strip() or "error"


async def _remote_content_hash(srv: dict, remote_path: str, is_file: bool) -> str:
    """服务器算目录或文件的内容 hash。和本地算法对称。"""
    cmd = (
        _FILE_SHA256_CMD.format(path=remote_path)
        if is_file
        else _DIR_CONTENT_HASH_CMD.format(path=remote_path)
    )
    rc, stdout, _ = await _ssh_exec_ubuntu(srv, cmd)
    return stdout.strip() or "error"


def _write_recipe_manifest(items: list[dict], run_dir: Path) -> dict:
    """L2a: 本地对每个 item 的 recipe 算 sha256，写 recipe_manifest.json。

    dispatch 后的 postcheck 用这个作为 ground truth 和服务器上 archive 文件对比。
    """
    import hashlib as _hl

    manifest: dict[str, dict] = {}
    for item in items:
        recipe_content = item.get("recipe", "") or ""
        if recipe_content:
            manifest[item["id"]] = {
                "recipe_hash": _hl.sha256(recipe_content.encode()).hexdigest()[:16],
                "recipe_length": len(recipe_content),
                "has_recipe": True,
            }
        else:
            manifest[item["id"]] = {
                "recipe_hash": "",
                "recipe_length": 0,
                "has_recipe": False,  # cc_no_recipe 模式，archive 应为空
            }
    manifest_file = run_dir / "recipe_manifest.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


async def _verify_recipe_archives(
    servers: list[dict],
    items: list[dict],
    run_name: str,
    recipe_mode: str,
    local_manifest: dict,
) -> dict:
    """L2b: dispatch 完成后 SSH 每台服务器读 archive，算 hash 对比本地 manifest。

    对 cc_with_recipe / cc_no_recipe：
      - 读 /tmp/caw-eval-recipes/{run}/{item}.json
      - 抽 result.data.results[0].content（空则视为 no_recipe）
      - sha256 对比本地 manifest
    任一 item 在任一服务器不一致 → 记录到 mismatches。
    """
    if recipe_mode not in ("cc_with_recipe", "cc_no_recipe"):
        return {"mode": recipe_mode, "skipped": True}

    results: dict = {"mode": recipe_mode, "mismatches": [], "all_match": True, "details": {}}

    # 服务器端算 archive 的 content hash（Python inline）
    def _probe_cmd(remote_path: str) -> str:
        return (
            "python3 - <<'PYEOF'\n"
            "import json, hashlib, sys\n"
            f"path = '{remote_path}'\n"
            "try:\n"
            "    d = json.load(open(path))\n"
            "    results = d['result']['data']['results']\n"
            "    content = results[0]['content'] if results else ''\n"
            "    print(f'hash={hashlib.sha256(content.encode()).hexdigest()[:16]} len={len(content)}')\n"
            "except Exception as e:\n"
            "    print(f'error={e}')\n"
            "PYEOF"
        )

    for srv in servers:
        srv_name = srv["name"]
        results["details"][srv_name] = {}
        for item in items:
            item_id = item["id"]
            expected = local_manifest.get(item_id, {})
            remote_path = f"/tmp/caw-eval-recipes/{run_name}/{item_id}.json"
            rc, stdout, _ = await _ssh_exec_ubuntu(srv, _probe_cmd(remote_path))
            line = stdout.strip().splitlines()[-1] if stdout.strip() else ""

            if line.startswith("error="):
                # archive 文件不存在或解析失败
                match = False
                actual_hash = ""
                actual_len = 0
                err = line[6:]
            elif line.startswith("hash="):
                # 解析 `hash=xxx len=N`
                parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
                actual_hash = parts.get("hash", "")
                actual_len = int(parts.get("len", "0"))
                match = actual_hash == expected.get("recipe_hash", "")
                err = ""
            else:
                match = False
                actual_hash = ""
                actual_len = 0
                err = f"unexpected output: {line!r}"

            results["details"][srv_name][item_id] = {
                "expected_hash": expected.get("recipe_hash", ""),
                "actual_hash": actual_hash,
                "expected_len": expected.get("recipe_length", 0),
                "actual_len": actual_len,
                "match": match,
                "error": err,
            }
            if not match:
                results["mismatches"].append(f"{item_id}@{srv_name}")
                results["all_match"] = False

    return results


async def _precheck_servers(
    servers: list[dict], components: list[str] = None
) -> tuple[bool, dict[str, dict]]:
    """R2: dispatch 前强制 precheck —— 对比本地 vs 各服务器的**内容 hash**。

    使用跨平台对称的 find + sort + shasum 算法（bsdtar / gnu tar 兼容性问题已规避）。
    内容完全一致 → 两端 hash 相同；任何字节差异 → hash 不同 → abort。

    返回 (all_match, version_info)。all_match=False 表示至少一台服务器和本地内容不一致。
    """
    # 本地内容 hash
    # _SCRIPTS_DIR = <repo>/cobo-agent-wallet/sdk/skills/caw-eval/scripts
    #   parents[0] = caw-eval
    #   parents[1] = skills     ← sandbox skill 在这下面
    #   parents[2] = sdk        ← go/ caw 源码和 build 产物在这下面
    local_skills_dir = _SCRIPTS_DIR.parents[1]
    local_sdk_dir = _SCRIPTS_DIR.parents[2]
    local_paths = {
        "skill": (local_skills_dir / "cobo-agentic-wallet-sandbox", False),
        "scripts": (_SCRIPTS_DIR, False),
    }
    # caw 二进制：如果本地编译了才对比；否则标 skipped
    local_caw_bin = local_sdk_dir / "go" / "build" / "bin" / "caw"
    if local_caw_bin.exists():
        local_paths["caw-binary"] = (local_caw_bin, True)

    local_hashes: dict[str, str] = {}
    for comp, (path, is_file) in local_paths.items():
        local_hashes[comp] = _local_content_hash(path, is_file)

    # 额外采集本地 git hash 作为 snapshot 记录（不用于 precheck 比较，但写入 deployment_snapshot）
    repo_root = _SCRIPTS_DIR.parents[3]
    local_git_hashes: dict[str, str] = {}
    for comp, path in [
        ("skill", "cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-sandbox"),
        ("scripts", "cobo-agent-wallet/sdk/skills/caw-eval/scripts"),
        ("caw", "cobo-agent-wallet/sdk/go"),
    ]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                f"HEAD:{path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            local_git_hashes[comp] = stdout.decode().strip() if proc.returncode == 0 else ""
        except (asyncio.TimeoutError, OSError):
            local_git_hashes[comp] = ""

    print("  [local content hashes]")
    for comp, h in local_hashes.items():
        print(f"    {comp:12s}: {h[:16]}")

    # 服务器各组件 content hash
    remote_paths = {
        "skill": ("~/.agents/skills/cobo-agentic-wallet-sandbox", False),
        "scripts": ("~/.agents/skills/caw-eval/scripts", False),
        "caw-binary": ("~/.cobo-agentic-wallet/bin/caw", True),
    }
    server_hashes: dict[str, dict] = {}
    all_match = True

    for srv in servers:
        kv: dict[str, str] = {}
        for comp, (path, is_file) in remote_paths.items():
            kv[comp] = await _remote_content_hash(srv, path, is_file)
        server_hashes[srv["name"]] = kv

        print(f"  [{srv['name']}]")
        for comp, remote_h in kv.items():
            local_h = local_hashes.get(comp, "skipped")
            if local_h in ("missing", "skipped"):
                # 本地没编译 caw 这种情况，只记录不对比
                print(f"    {comp:12s}: remote={remote_h[:16]} (local {local_h}, skip compare)")
                continue
            match = local_h == remote_h
            mark = "✓" if match else "✗"
            print(f"    {comp:12s}: local={local_h[:16]} remote={remote_h[:16]} [{mark}]")
            if not match:
                all_match = False

    return all_match, {
        "local_content_hashes": local_hashes,
        "local_git_hashes": local_git_hashes,
        "servers": server_hashes,
    }


async def _cmd_dispatch(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    servers: list[dict],
    timeout: int,
    model: str,
    eval_mode: str,
    recipe_mode: str,
    sync_scripts: bool,
    force: bool,
    precheck: bool = True,
) -> None:
    """本地 Mac 端：并行 dispatch CC 评测到多台服务器（动态队列）。

    流程：
      1. busy check：并行探 N 台，跳过有 claude -p / openclaw agent eval- 在跑的机器
      2. precheck（R2）：对比本地 vs 服务器组件存在性 + caw version；不一致 abort（可 --no-precheck 关闭）
      3. （可选）rsync 本地 scripts/ 到空闲机器的 ~/.agents/skills/caw-eval/scripts/
      4. 采集 deployment_snapshot（R3）写入 run manifest
      5. 动态队列：每台空闲机器作为 worker 持续取 item 执行
      6. 每个 item 执行完 scp 拉 session 回本地 run_dir
    """
    from runtime_compliance import assert_pre_run

    assert_pre_run()

    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]
    if not items:
        print("[ERROR] 没有匹配的 items")
        sys.exit(1)
    if not servers:
        print("[ERROR] 至少需要一台 --server")
        sys.exit(1)

    local_run_dir = _RUNS_DIR / run_name
    log_dir = local_run_dir / "dispatch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== CC Dispatch [dynamic] (run: {run_name}) ===")
    print(f"数据集: {dataset_name} ({len(items)} items)")
    print(f"候选服务器: {len(servers)}")
    print(f"模型: {model} eval_mode={eval_mode} recipe_mode={recipe_mode or '-'}")

    print("\n=== 1/3 busy check ===")
    probes = await asyncio.gather(*(_check_server_busy(s) for s in servers))
    free_servers: list[dict] = []
    for srv, state in zip(servers, probes):
        mark = "FREE" if state == "free" else f"SKIP ({state})"
        print(f"  {srv['name']}: {mark}")
        if state == "free" or (force and state != "error"):
            free_servers.append(srv)
    if not free_servers:
        print("[ERROR] 所有服务器 busy/error。用 --force 强制跑（不推荐）")
        sys.exit(2)

    if sync_scripts:
        print(f"\n=== 2/4 同步 scripts/ 到 {len(free_servers)} 台 ===")
        src = _SCRIPTS_DIR
        sync_results = await asyncio.gather(
            *(_sync_scripts_to_server(s, src) for s in free_servers),
            return_exceptions=True,
        )
        ok_servers: list[dict] = []
        for srv, r in zip(free_servers, sync_results):
            if isinstance(r, Exception):
                print(f"  {srv['name']}: 同步异常 {r}")
            elif r:
                print(f"  {srv['name']}: 同步完成")
                ok_servers.append(srv)
        if not ok_servers:
            print("[ERROR] 没有服务器同步成功")
            sys.exit(3)
        free_servers = ok_servers

    # R2 + R3: precheck 内容 hash 对比 + 采集 deployment_snapshot
    deployment_snapshot: dict = {}
    if precheck:
        print("\n=== 3/4 precheck（本地 vs 服务器 content hash 对比）===")
        all_match, versions = await _precheck_servers(free_servers)
        deployment_snapshot = {
            "run_name": run_name,
            "dataset_name": dataset_name,
            "model": model,
            "local_content_hashes": versions.get("local_content_hashes", {}),
            "local_git_hashes": versions.get("local_git_hashes", {}),
            "servers": versions.get("servers", {}),
            "eval_mode": eval_mode,
            "recipe_mode": recipe_mode,
            "collected_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        if not all_match:
            if force:
                print("[WARN] precheck 发现内容不一致，--force 继续（不推荐，会污染归因）")
            else:
                print("[ERROR] precheck 失败：服务器组件和本地内容不一致。")
                print(
                    "        请先 sync_to_servers.sh 同步后重跑，或 --force 强制继续（会标记 run 不可信）"
                )
                sys.exit(4)
        snapshot_file = local_run_dir / "deployment_snapshot.json"
        snapshot_file.write_text(
            json.dumps(deployment_snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  [OK] snapshot -> {snapshot_file.name}")
    else:
        print("\n=== 3/4 precheck SKIPPED（--no-precheck） ===")

    # L2a: dispatch 前写本地 recipe_manifest.json（recipe hash 的 ground truth）
    recipe_manifest = _write_recipe_manifest(items, local_run_dir)
    if recipe_mode in ("cc_with_recipe", "cc_no_recipe"):
        print(
            f"  [OK] recipe_manifest.json -> {len(recipe_manifest)} items "
            f"(with_recipe={sum(1 for v in recipe_manifest.values() if v['has_recipe'])})"
        )

    print(f"\n=== 4/4 动态分发 {len(items)} items 给 {len(free_servers)} 台 ===")
    queue: asyncio.Queue = asyncio.Queue()
    item_map = {it["id"]: it for it in items}
    for it in items:
        await queue.put(it["id"])
    item_results: dict[str, tuple[str, str]] = {}

    workers = [
        _dispatch_worker_cc(
            srv,
            queue,
            item_map,
            item_results,
            run_name,
            timeout,
            model,
            eval_mode,
            recipe_mode,
            log_dir,
            local_run_dir,
        )
        for srv in free_servers
    ]
    await asyncio.gather(*workers)

    # L2b: postcheck —— SSH 读服务器上的 archive，对比本地 manifest
    if recipe_mode in ("cc_with_recipe", "cc_no_recipe"):
        print("\n=== Recipe archive postcheck（本地 dataset vs 服务器 archive hash 对比）===")
        verify_result = await _verify_recipe_archives(
            free_servers, items, run_name, recipe_mode, recipe_manifest
        )
        all_match = verify_result.get("all_match", True)
        mismatches = verify_result.get("mismatches", [])
        if all_match:
            print(f"  [OK] 所有 {len(items)}×{len(free_servers)} archive hash 和 dataset 一致")
        else:
            print(
                f"  [FAIL] {len(mismatches)} 处不一致：{mismatches[:5]}{'...' if len(mismatches) > 5 else ''}"
            )
            print("         具体差异见 deployment_snapshot.json / recipe_verification 段")
        # 写入 deployment_snapshot
        snapshot_file = local_run_dir / "deployment_snapshot.json"
        if snapshot_file.exists():
            try:
                snap = json.loads(snapshot_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                snap = {}
        else:
            snap = {}
        snap["recipe_verification"] = verify_result
        snapshot_file.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "cc-dispatch",
        "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        "model": model,
        "eval_mode": eval_mode,
        "recipe_mode": recipe_mode,
        "items": {
            iid: {
                "status": item_results.get(iid, ("-", "skipped"))[1],
                "server": item_results.get(iid, ("-", ""))[0],
                "operation_type": item_map[iid].get("operation_type", ""),
                "difficulty": item_map[iid].get("difficulty", ""),
            }
            for iid in item_map
        },
    }
    (local_run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n=== 完成 ===")
    failed = [(iid, s) for iid, (_, s) in item_results.items() if s != "ok"]
    for iid, (srv_name, st) in sorted(item_results.items()):
        print(f"  [{srv_name}] {iid}: {st}")
    if failed:
        print(f"\n失败: {len(failed)}/{len(item_results)}")
        print(f"日志: {log_dir}/<server>-<item>.log")
    else:
        print(f"\n所有 {len(item_results)} item 执行完毕")
    print(f"\nrun 目录: {local_run_dir}")


# ── metrics 子命令 ────────────────────────────────────────────────────────────


def _extract_session_metrics(jsonl_path: Path) -> dict:
    """从单个 session JSONL 文件提取运行指标。

    返回字段：
      duration_secs, tokens, tool_calls, caw_cmds, pact_submits, tx_cmds, errors
    """
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    timestamps: list[str] = []
    total_tokens = 0
    tool_call_count = 0

    # caw 命令记录：{id, command, is_pact_submit, is_tx}
    caw_records: list[dict] = []

    # tool_result 索引：tool_use_id -> result_text
    result_index: dict[str, str] = {}

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = ev.get("timestamp", "")
        if ts:
            timestamps.append(ts)

        ev_type = ev.get("type", "")
        msg = ev.get("message", {})
        if not isinstance(msg, dict):
            continue

        if ev_type == "user":
            # 收集 tool_result
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    raw_content = block.get("content", "")
                    if isinstance(raw_content, list):
                        text = "\n".join(
                            b.get("text", "") for b in raw_content if isinstance(b, dict)
                        )
                    else:
                        text = str(raw_content)
                    result_index[block["tool_use_id"]] = text

        elif ev_type == "assistant":
            # 累计 tokens：output_tokens（模型生成量，不受 cache 影响，最能反映实际工作量）
            usage = msg.get("usage", {})
            total_tokens += usage.get("output_tokens", 0)

            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_call_count += 1
                if block.get("name") != "Bash":
                    continue
                inp = block.get("input", {})
                cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                # 只统计实际 caw 命令（排除 PATH export 等前缀）
                if not re.search(r"\bcaw\s+\w", cmd):
                    continue
                caw_records.append(
                    {
                        "id": block.get("id", ""),
                        "command": cmd,
                        "is_pact_submit": bool(re.search(r"\bcaw\s+pact\s+submit\b", cmd)),
                        "is_tx": bool(re.search(r"\bcaw\s+tx\b", cmd)),
                    }
                )

    # 时长
    duration_secs = 0
    if len(timestamps) >= 2:
        t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
        duration_secs = int((t1 - t0).total_seconds())

    # 错误数：caw 命令返回 error_code 或 "error": true
    error_count = 0
    for rec in caw_records:
        result = result_index.get(rec["id"], "")
        is_error = False
        try:
            data = json.loads(result)
            inner = data.get("result", data)
            is_error = bool(inner.get("error_code")) or bool(data.get("error"))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass
        if not is_error:
            lower = result.lower()
            is_error = '"error": true' in lower or '"error_code"' in lower
        if is_error:
            error_count += 1

    mins, secs = divmod(duration_secs, 60)
    return {
        "duration_secs": duration_secs,
        "duration_str": f"{mins}:{secs:02d}",
        "tokens": total_tokens,
        "tool_calls": tool_call_count,
        "caw_cmds": len(caw_records),
        "pact_submits": sum(1 for r in caw_records if r["is_pact_submit"]),
        "tx_cmds": sum(1 for r in caw_records if r["is_tx"]),
        "errors": error_count,
    }


def cmd_metrics(run_name: str) -> None:
    """从 run 目录的 session 文件提取运行指标，写入 session_metrics.json。"""
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] run 目录不存在: {run_dir}")
        sys.exit(1)

    session_files = sorted(run_dir.glob("E2E-*.jsonl"))
    if not session_files:
        print(f"[ERROR] 没有找到 session 文件: {run_dir}")
        sys.exit(1)

    print(f"=== 提取运行指标 ({len(session_files)} 个 session) ===\n")

    items: list[dict] = []
    for sf in session_files:
        m = _extract_session_metrics(sf)
        m["item_id"] = sf.stem
        items.append(m)
        print(
            f"  [{sf.stem}]  {m['duration_str']:>6s}  "
            f"tokens={m['tokens']:>7,}  tool={m['tool_calls']:>3}  "
            f"caw={m['caw_cmds']:>3}  pact_sub={m['pact_submits']}  "
            f"tx={m['tx_cmds']}  err={m['errors']}"
        )

    # 合计 / 平均
    def _sum(key: str) -> int:
        return sum(it[key] for it in items)

    n = len(items)
    totals = {
        "duration_secs": _sum("duration_secs"),
        "tokens": _sum("tokens"),
        "tool_calls": _sum("tool_calls"),
        "caw_cmds": _sum("caw_cmds"),
        "pact_submits": _sum("pact_submits"),
        "tx_cmds": _sum("tx_cmds"),
        "errors": _sum("errors"),
    }
    tm, ts_ = divmod(totals["duration_secs"], 60)
    totals["duration_str"] = f"{tm}:{ts_:02d}"

    averages = {k: round(v / n, 1) for k, v in totals.items() if k not in ("duration_str",)}
    am, as_ = divmod(int(averages["duration_secs"]), 60)
    averages["duration_str"] = f"{am}:{as_:02d}"

    def _fmt(d: dict) -> str:
        return (
            f"{d['duration_str']}  tokens={d['tokens']:,}  tool={d['tool_calls']}"
            f"  caw={d['caw_cmds']}  pact_sub={d['pact_submits']}"
            f"  tx={d['tx_cmds']}  err={d['errors']}"
        )

    print(f"\n  合计: {_fmt(totals)}")
    print(f"  平均: {_fmt(averages)}")

    output = {
        "run_name": run_name,
        "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": items,
        "totals": totals,
        "averages": averages,
    }
    out_path = run_dir / "session_metrics.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n已写入: {out_path}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code 评测编排脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    # ── upload ────────────────────────────────────────────────────────────────
    p_upload = sub.add_parser("upload", help="批量上传 session 到 Langfuse")
    p_upload.add_argument("--run-name", required=True)
    p_upload.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_upload.add_argument("--item-id", nargs="*", help="只上传指定 item")
    p_upload.add_argument("--skill", default="cobo-agentic-wallet-dev")
    p_upload.add_argument(
        "--model", default="sonnet", help="模型短标识，用于 run description（如 sonnet）"
    )
    p_upload.add_argument(
        "--model-full", default="claude-sonnet-4-6", help="完整模型 ID，写入 run description"
    )
    p_upload.add_argument(
        "--description", default="", help="自定义 run description（覆盖自动生成）"
    )
    p_upload.add_argument(
        "--no-link",
        action="store_true",
        help="只上传 trace，不创建/关联 dataset run（指定少量 case 调试时用）",
    )

    # ── score ─────────────────────────────────────────────────────────────────
    p_score = sub.add_parser("score", help="对 session 评分")
    p_score.add_argument("--run-name", required=True)
    p_score.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_score.add_argument("--report", action="store_true", help="打印评分报告")
    p_score.add_argument("--dump-judge-requests", help="导出 LLM judge 请求到文件")
    p_score.add_argument("--judge-results", help="读取 LLM judge 结果文件")

    # ── import-sessions ──────────────────────────────────────────────────────
    p_import = sub.add_parser("import-sessions", help="从外部目录导入 session 文件")
    p_import.add_argument(
        "--from", dest="from_dir", required=True, help="源目录（如 /tmp/oc-sessions/）"
    )
    p_import.add_argument("--run-name", required=True, help="导入到的 run 名称")

    # ── metrics ───────────────────────────────────────────────────────────────
    p_metrics = sub.add_parser(
        "metrics", help="从 session 文件提取运行指标（时长/tokens/caw命令等）"
    )
    p_metrics.add_argument("--run-name", required=True)

    # ── run（服务器端 headless 执行单 item / 小批量）───────────────────────────
    p_run = sub.add_parser("run", help="服务器端：headless 逐 item 执行评测")
    p_run.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_run.add_argument("--run-name", required=True)
    p_run.add_argument("--item-id", nargs="*", help="只跑指定 item")
    p_run.add_argument("--timeout", type=int, default=_DEFAULT_CC_TIMEOUT, help="单 item 超时秒数")
    p_run.add_argument("--model", default="sonnet", help="claude --model 传入的模型标识")
    p_run.add_argument("--eval-mode", choices=["standard", "recipe"], default="standard")
    p_run.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "openclaw"],
        default="",
    )
    p_run.add_argument(
        "--inline-item",
        default=None,
        help="inline 模式：直接传 item JSON，免配 Langfuse。"
        ' 格式：{"id":"...","user_message":"...","operation_type":"...","difficulty":"...","recipe":"...",...}',
    )

    # ── dispatch（本地 Mac 端：并行调度多台服务器跑 CC 评测）───────────────────
    p_dispatch = sub.add_parser(
        "dispatch",
        help="本地 Mac 端：并行调度 N 台服务器，动态队列分发 item（含 busy check）",
    )
    p_dispatch.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_dispatch.add_argument("--run-name", required=True)
    p_dispatch.add_argument("--item-id", nargs="*", help="只跑指定 item")
    p_dispatch.add_argument(
        "--server",
        action="append",
        required=True,
        metavar="name:zone:project",
        help="gcloud 服务器规格，可重复；会先 busy check 再分配",
    )
    p_dispatch.add_argument(
        "--timeout", type=int, default=_DEFAULT_CC_TIMEOUT, help="远端单 item 超时"
    )
    p_dispatch.add_argument("--model", default="sonnet")
    p_dispatch.add_argument("--eval-mode", choices=["standard", "recipe"], default="standard")
    p_dispatch.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "openclaw"],
        default="",
    )
    p_dispatch.add_argument(
        "--no-sync-scripts",
        action="store_true",
        help="跳过 scripts/ 同步（假设服务器已是最新版本）",
    )
    p_dispatch.add_argument(
        "--force",
        action="store_true",
        help="即使服务器 busy 或 precheck 不一致也照跑（不推荐，会污染正式评测结果）",
    )
    p_dispatch.add_argument(
        "--no-precheck",
        action="store_true",
        help="跳过 R2 precheck（组件版本校验 + deployment_snapshot 采集）。正式评测禁用此项",
    )

    args = parser.parse_args()

    if args.cmd == "upload":
        cmd_upload(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            item_ids=args.item_id,
            skill=args.skill,
            model=args.model,
            model_full=args.model_full,
            description=args.description,
            skip_link=args.no_link,
        )
    elif args.cmd == "score":
        cmd_score(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            report=args.report,
            dump_judge=args.dump_judge_requests,
            judge_results=args.judge_results,
        )
    elif args.cmd == "import-sessions":
        cmd_import_sessions(
            from_dir=args.from_dir,
            run_name=args.run_name,
        )
    elif args.cmd == "metrics":
        cmd_metrics(run_name=args.run_name)
    elif args.cmd == "run":
        asyncio.run(
            _cmd_run(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                timeout=args.timeout,
                model=args.model,
                eval_mode=args.eval_mode,
                recipe_mode=args.recipe_mode,
                inline_item=args.inline_item,
            )
        )
    elif args.cmd == "dispatch":
        servers = [_parse_server_spec(s) for s in args.server]
        asyncio.run(
            _cmd_dispatch(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                servers=servers,
                timeout=args.timeout,
                model=args.model,
                eval_mode=args.eval_mode,
                recipe_mode=args.recipe_mode,
                sync_scripts=not args.no_sync_scripts,
                force=args.force,
                precheck=not args.no_precheck,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
