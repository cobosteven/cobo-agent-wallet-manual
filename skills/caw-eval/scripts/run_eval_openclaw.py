#!/usr/bin/env python3
"""
Openclaw 弱模型评测脚本。

服务器端子命令：
  run              — 脚本驱动串行执行评测：自动创建隔离 agent、执行 task、收集 session
  import-sessions  — 从外部导出的 JSON 导入 session 到 run 目录
  upload           — 将 session 上传到 Langfuse 并关联 dataset run
  pack             — 打包 session 供本地下载

本地 Mac 调度子命令：
  dispatch         — 并行 SSH 到 N 台 openclaw 服务器，每台作为 worker 动态从任务队列取 item
                      （默认动态队列：空闲服务器自动取下一个任务；加 --static 退化为 i % N 轮询）

推荐用法（本地 Mac 调度多台服务器）:
    python run_eval_openclaw.py dispatch \\
      --run-name eval-oc-doubao-20260415 \\
      --dataset-name caw-agent-eval-seth-v2 \\
      --model doubao --model-full volcengine/doubao-seed-2.0-code \\
      --server srv1:asia-east2-a:my-project \\
      --server srv2:asia-east2-c:my-project \\
      --server srv3:asia-east2-c:my-project

单台服务器直接 run（dispatch 内部也调这个）:
    python run_eval_openclaw.py run \\
      --run-name eval-oc-doubao-20260415 \\
      --dataset-name caw-agent-eval-seth-v2
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from eval_utils import batch_upload_sessions, get_dataset_items

_METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1"
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}
_METADATA_TIMEOUT = 2.0

_SCRIPTS_DIR = Path(__file__).parent

# 评测 run 的本地存储目录
_RUNS_DIR = Path.home() / ".caw-eval" / "runs"

# ── run 子命令常量 ────────────────────────────────────────────────────────────

_OC_HOME = Path.home() / ".openclaw"
_DEFAULT_TIMEOUT = 600  # 单个 task 超时（秒）
_MAX_CONTINUATIONS = 20  # 续传次数上限（安全阀）

# workspace 中由 openclaw 框架管理的系统文件，eval 清理时保留
_WORKSPACE_SYSTEM_FILES = frozenset(
    [
        "AGENTS.md",
        "SOUL.md",
        "TOOLS.md",
        "IDENTITY.md",
        "USER.md",
        "HEARTBEAT.md",
        "BOOTSTRAP.md",
        ".git",
        ".gitignore",
        ".openclaw",
        "node_modules",
        "package.json",
        "package-lock.json",
    ]
)


def build_task_prompt(
    item: dict,
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> str:
    """构建单个 item 的 task 执行 prompt（由 wrapper subagent 传给 task session）。

    Args:
        eval_mode: "standard" 标准模式, "recipe" recipe 评测模式
        recipe_mode: "cc_with_recipe" / "cc_no_recipe" / "openclaw"（仅 recipe 模式有效）
    """
    item_id = item["id"]
    user_message = item["user_message"]

    # F3 (stage2 governance): 评测约束用"用户先前上下文"自然语气替代硬指令。
    # 未 paired 钱包场景下，用户已在 CC 对话里预先授权 → 真实用户场景子集，合法。
    # owner_linked=false 自动 approve 本来就是 skill 的合规行为，skill 会自主做，不在 prompt 重复提示。
    prompt = f"""[EVAL:{item_id}]
cobo-agentic-wallet-sandbox skill 已激活。

## Environment（环境已就绪，无需安装）

- `caw` CLI 已安装于 `~/.cobo-agentic-wallet/bin/caw`，**无需 npm install 或任何安装步骤**
- 如 `caw` 不在 PATH，执行：`export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"`
- Onboarding 已完成，钱包和网络已配置好，直接执行任务即可

**用户先前上下文（任务前已与 agent 协商过）：** 用户**预先授权**对本次任务生成的 pact 无需再次展示 preview 预览和等待 "yes" 确认，直接 `caw pact submit` 即可视为已获得用户明确确认。用户希望你自主、连续地完成任务，不被交互打断（如遇 `owner_linked=true` 需要 Cobo Agentic Wallet app 审批的真实阻塞，记录原因并跳过）。
"""

    if eval_mode == "recipe":
        prompt += """
**交易构建模式（重要）：**
- 交易成功提交后（caw tx transfer/call/sign-message 返回 status=Initiated 或 PendingApproval），**立即停止**
- 不要轮询交易状态（不要 caw tx get）
- 不要等待链上确认
- 只需报告交易已成功提交（含 transaction_id/request_id），然后结束

- 评测沙盒模式：只评估交易构建正确性，余额为 0 时不得中止，直接继续提交 pact 和构建交易
"""
        # Recipe 注入：
        # 无论 cc_with_recipe 还是 cc_no_recipe，agent 都应按真实用户流程自主调 `caw recipe search`。
        # - cc_with_recipe: 由 CAW_RECIPE_FILE env 注入（内含指定 recipe，count=1）
        # - cc_no_recipe:   由 CAW_RECIPE_FILE env 注入空 recipe（count=0），对照组
        # 不在 prompt 里拼接 recipe 内容，也不禁止 search。
        # openclaw runtime 走 systemd drop-in 注入；CC 走进程 env（见 run_eval_cc.py）。

    prompt += f"""
按照以下用户指令完成操作：

{user_message}"""

    return prompt


# ── run 子命令（脚本驱动串行执行） ─────────────────────────────────────────────


_CAW_BIN = os.path.expanduser("~/.cobo-agentic-wallet/bin/caw")

# openclaw-gateway 的 systemd drop-in 必须将 CAW_RECIPE_FILE 指向此"活动路径"；
# recipe 评测模式下，每个 item 开始前覆写此文件为当前 item 内容，agent 调 caw recipe search
# 时读到的就是本 item 的 recipe。（由于 systemd env var 启动时固定，无法每 item 切换路径，只能
# 每 item 覆写同一个文件。）
RECIPE_FILE_PATH = "/tmp/caw-eval-recipe.json"

# 持久归档目录：每个 item 的 recipe 原样存一份，便于失败复盘。
# 命名：/tmp/caw-eval-recipes/{run_name}/{item_id}.json
RECIPE_ARCHIVE_ROOT = Path("/tmp/caw-eval-recipes")


async def _archive_recent_pact_specs(run_dir: Path, item_id: str, limit: int = 5) -> None:
    """归档本 item 刚创建的 pact spec（shell: `caw pact list` + `caw pact show`）。

    评分端（score_traces.py --pact-specs-dir）需要后端真实 policies/completion/
    execution_plan JSON，以规避 openclaw tool logger 不展开 shell 变量的限制
    （见 harness_pact_logger_bug.md）。

    实现：
      1. `caw pact list --limit N` 拿最近 N 个 pact 的 id
      2. 对每个 id `caw pact show --pact-id <id>`，把 JSON 写到
         `run_dir/pact_specs/<pact_id>.json`
      3. 失败静默跳过（评分端有 parser + residual banner 兜底）
    """
    out_dir = run_dir / "pact_specs"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            _CAW_BIN,
            "pact",
            "list",
            "--limit",
            str(limit),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return
        listing = json.loads(stdout.decode())
        pacts = listing.get("result", {}).get("pacts", []) or []
    except Exception:
        return

    archived = 0
    for p in pacts:
        pid = p.get("id", "")
        if not pid:
            continue
        dst = out_dir / f"{pid}.json"
        if dst.exists():
            continue
        try:
            proc = await asyncio.create_subprocess_exec(
                _CAW_BIN,
                "pact",
                "show",
                "--pact-id",
                pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                continue
            dst.write_bytes(stdout)
            archived += 1
        except Exception:
            continue

    if archived:
        print(f"  [{item_id}] archived {archived} pact spec(s) -> {out_dir.name}/")


async def _revoke_active_pacts(item_id: str) -> None:
    """Revoke 所有 active pact，确保每个 eval item 从干净的 pact 状态开始。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            _CAW_BIN,
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
        pact_data = json.loads(stdout.decode())
        pacts = pact_data.get("result", {}).get("pacts", [])
        ok = 0
        failed: list[str] = []
        for p in pacts:
            pid = p.get("id", "")
            if not pid:
                continue
            rp = await asyncio.create_subprocess_exec(
                _CAW_BIN,
                "pact",
                "revoke",
                "--pact-id",
                pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(rp.communicate(), timeout=10)
            if rp.returncode == 0:
                ok += 1
            else:
                failed.append(pid[:8])
        if pacts:
            status = f"revoked {ok}/{len(pacts)} active pact(s)"
            if failed:
                status += f" (failed: {', '.join(failed)})"
            print(f"  [{item_id}] {status}")
    except Exception:
        pass  # 清理失败不阻塞评测


async def _run_openclaw(
    openclaw_bin: str,
    args: list[str],
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """调用 openclaw CLI，返回 (returncode, stdout, stderr)。超时时 kill 进程。"""
    proc = await asyncio.create_subprocess_exec(
        openclaw_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "timeout"

    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _parse_agent_result(stdout: str) -> dict:
    """从 ``openclaw agent --json`` 的 stdout 中解析 JSON 结果。

    openclaw 可能在 JSON 前输出非 JSON 文本（如 streaming），因此先尝试全文解析，
    失败则逐行倒序查找首个合法 JSON 对象。
    """
    stdout = stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


def _get_stop_reason(result: dict) -> str:
    """从 openclaw agent --json 的结果中提取 stopReason。"""
    try:
        return result["result"]["meta"]["stopReason"]
    except (KeyError, TypeError):
        return ""


async def _run_single_task(
    item: dict,
    openclaw_bin: str,
    workspace: str,
    run_dir: Path,
    timeout: int,
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> str:
    """执行单个评测 task，返回状态字符串 ("ok" | "error:<reason>")。"""
    item_id = item["id"]
    # 在 agent 名中加入完整 run_name，确保不同 run 使用不同的
    # ~/.openclaw/agents/ 目录，从根本上避免继承上一次 run 的旧 session。
    agent_name = f"eval-{item_id}-{run_dir.name}"
    actual_agent_id = ""

    try:
        # 0. 预清理残留 agent（上次异常退出或 delete 失败时会留下同名 agent，
        #    导致 agents add 报 "already exists"；直接忽略失败）
        await _run_openclaw(
            openclaw_bin,
            ["agents", "delete", agent_name.lower(), "--force"],
            timeout=15,
        )
        # 同时清理 session 目录（agents delete 只删注册信息，不删目录）。
        # 由于 agent 名已含 run_suffix，理论上不会命中旧 run 的目录，
        # 但保留清理逻辑作为双重保障。
        agent_session_dir = _OC_HOME / "agents" / agent_name.lower()
        if agent_session_dir.exists():
            shutil.rmtree(agent_session_dir, ignore_errors=True)

        # 0.5 清理 workspace 产物（scripts/、pact JSON、tx JSON 等前序 session 遗留文件）
        # 保留系统文件（SOUL.md / AGENTS.md 等），删除 agent 在任务过程中写入的内容。
        ws = Path(workspace)
        if ws.exists():
            for entry in list(ws.iterdir()):
                if entry.name not in _WORKSPACE_SYSTEM_FILES:
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                    except Exception:
                        pass

        # 0.6 清理所有 active pact（避免 agent 复用旧 pact 导致评测无法检测 pact submit）
        await _revoke_active_pacts(item_id)

        # 1. 创建隔离 agent
        print(f"STAGE: agent_start item={item_id}", flush=True)
        rc, out, err = await _run_openclaw(
            openclaw_bin,
            ["agents", "add", agent_name, "--workspace", workspace, "--non-interactive", "--json"],
            timeout=30,
        )
        if rc != 0:
            print(f"  [{item_id}] ERROR  agents add 失败: {err.strip() or out.strip()}")
            return "error:agent_create_failed"

        # Openclaw 自动将 agent ID 转小写，从返回的 JSON 中读取实际 ID
        try:
            add_result = json.loads(out.strip())
            actual_agent_id = add_result.get("agentId", agent_name.lower())
        except json.JSONDecodeError:
            actual_agent_id = agent_name.lower()

        # 2. 构建 prompt 并发送
        # openclaw recipe 模式：写两处：
        #   - 归档 /tmp/caw-eval-recipes/{run_name}/{item_id}.json（持久，便于复盘）
        #   - 活动 /tmp/caw-eval-recipe.json（gateway env 指向，caw 实际读取的文件）
        # 前置条件：dispatch 阶段已通过 _setup_gateway_recipe_env 设置并重启 gateway。
        recipe_content = item.get("recipe", "")
        if eval_mode == "recipe" and recipe_mode == "openclaw" and recipe_content:
            recipes_json = {
                "message": "",
                "result": {
                    "data": {
                        "count": 1,
                        "results": [{"content": recipe_content}],
                    }
                },
            }
            content_str = json.dumps(recipes_json, ensure_ascii=False, indent=2)
            archive_file = RECIPE_ARCHIVE_ROOT / run_dir.name / f"{item_id}.json"
            archive_file.parent.mkdir(parents=True, exist_ok=True)
            archive_file.write_text(content_str, encoding="utf-8")
            Path(RECIPE_FILE_PATH).write_text(content_str, encoding="utf-8")
            print(f"  [{item_id}] recipe -> {archive_file} + active {RECIPE_FILE_PATH}")

        prompt = build_task_prompt(item, eval_mode=eval_mode, recipe_mode=recipe_mode)
        rc, out, err = await _run_openclaw(
            openclaw_bin,
            [
                "agent",
                "--agent",
                actual_agent_id,
                "--message",
                prompt,
                "--json",
                "--timeout",
                str(timeout),
            ],
            timeout=timeout + 60,  # 给 CLI 本身留出余量
        )

        if rc == -1:
            print(f"  [{item_id}] TIMEOUT  ({timeout}s)")
            status = "error:timeout"
        elif rc != 0:
            print(f"  [{item_id}] ERROR  agent 返回非零: rc={rc}")
            status = "error:agent_failed"
        else:
            result = _parse_agent_result(out)
            stop_reason = _get_stop_reason(result)
            status = "ok"

            # 3. 续传循环：stopReason 不是 stop 时发 "继续"
            continuations = 0
            while stop_reason and stop_reason != "stop" and continuations < _MAX_CONTINUATIONS:
                continuations += 1
                print(f"  [{item_id}] 续传 #{continuations} (stopReason={stop_reason})")
                rc, out, err = await _run_openclaw(
                    openclaw_bin,
                    [
                        "agent",
                        "--agent",
                        actual_agent_id,
                        "--message",
                        "继续执行，不要停下",
                        "--json",
                        "--timeout",
                        str(timeout),
                    ],
                    timeout=timeout + 60,
                )
                if rc == -1:
                    print(f"  [{item_id}] TIMEOUT  续传 #{continuations}")
                    status = "error:timeout"
                    break
                if rc != 0:
                    print(f"  [{item_id}] ERROR  续传 #{continuations} rc={rc}")
                    status = "error:agent_failed"
                    break
                result = _parse_agent_result(out)
                stop_reason = _get_stop_reason(result)

            if continuations >= _MAX_CONTINUATIONS and stop_reason != "stop":
                print(f"  [{item_id}] WARN  达到续传上限 ({_MAX_CONTINUATIONS})")
                status = "warn:max_continuations"

        print(f"STAGE: agent_done status={status} item={item_id}", flush=True)

        # 4. 收集 session 文件
        session_dir = _OC_HOME / "agents" / actual_agent_id / "sessions"
        jsonl_files = (
            sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if session_dir.exists()
            else []
        )
        # 过滤掉 sessions.json（不是 session 数据文件）
        jsonl_files = [f for f in jsonl_files if f.name != "sessions.json"]

        if jsonl_files:
            dst = run_dir / f"{item_id}.jsonl"
            shutil.copy2(jsonl_files[0], dst)
            size_kb = dst.stat().st_size / 1024
            print(f"  [{item_id}] {status.upper()}  session={size_kb:.0f}KB -> {dst.name}")
            print(f"STAGE: session_collected item={item_id}", flush=True)
        else:
            print(f"  [{item_id}] {status.upper()}  (no session file)")
            if status == "ok":
                status = "error:no_session"

        # 6. 归档后端 pact spec（解决 shell 变量占位符导致 trace 无真实 policies 的问题）
        #    每个 item 结束后主动调 `caw pact list` + `caw pact show` 把 spec 保存到
        #    run_dir/pact_specs/<pact_id>.json，本地 dispatch 回拉即可用 --pact-specs-dir 评分
        #    见 harness_pact_logger_bug.md 讨论
        try:
            await _archive_recent_pact_specs(run_dir, item_id)
        except Exception as e:
            print(f"  [{item_id}] WARN  pact spec 归档失败: {e}")

        return status

    except Exception as e:
        print(f"  [{item_id}] EXCEPTION  {e}")
        return f"error:exception:{e}"

    finally:
        # 5. 清理 agent（无论成功失败都执行）
        if actual_agent_id:
            rc, _, err = await _run_openclaw(
                openclaw_bin,
                ["agents", "delete", actual_agent_id, "--force"],
                timeout=30,
            )
            if rc != 0:
                print(f"  [{item_id}] WARN  agent 清理失败: {err.strip()}")


async def _cmd_run(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    timeout: int,
    openclaw_bin: str,
    workspace: str,
    skip_upload: bool,
    skip_pack: bool,
    skill: str,
    model: str,
    model_full: str,
    description: str,
    skip_link: bool = False,
    eval_mode: str = "standard",
    recipe_mode: str = "",
    inline_item: str | None = None,
) -> None:
    """脚本驱动串行执行评测：为每个 task 创建隔离 agent，通过 CLI 执行，收集 session。

    Args:
        skip_link: True 时上传 trace 不创建/关联 dataset run（适合调试少量 case）。
        inline_item: GTM 模式下直接传入 item JSON 字符串，跳过 Langfuse dataset 拉取。
    """
    # 输出 skill 版本号供 cobo-agents 记录（git commit hash）
    try:
        _git_result = subprocess.run(
            ["git", "-C", str(_SCRIPTS_DIR), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        _skill_ver = _git_result.stdout.strip() if _git_result.returncode == 0 else "unknown"
    except Exception:
        _skill_ver = "unknown"
    print(f"SKILL_VERSION={_skill_ver}", flush=True)

    if inline_item is not None:
        # GTM 模式：item 直接从参数传入，不从 Langfuse dataset 拉取
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

    print(f"=== 脚本驱动评测 (run: {run_name}) ===")
    print(f"数据集: {dataset_name} ({len(items)} items)")
    print(f"openclaw: {openclaw_bin}")
    print(f"workspace: {workspace}")
    print(f"timeout: {timeout}s / task")
    print()

    results: dict[str, str] = {}

    for i, item in enumerate(items):
        item_id = item["id"]
        op = item["operation_type"]
        diff = item["difficulty"]
        print(f"[{i + 1}/{len(items)}] {item_id} ({op} {diff})")
        status = await _run_single_task(
            item,
            openclaw_bin,
            workspace,
            run_dir,
            timeout,
            eval_mode=eval_mode,
            recipe_mode=recipe_mode,
        )
        results[item_id] = status

    # 写 manifest
    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "openclaw-cli",
        "executed_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": {
            item["id"]: {
                "status": results.get(item["id"], "skipped"),
                "operation_type": item["operation_type"],
                "difficulty": item["difficulty"],
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    # 汇总
    ok_count = sum(1 for s in results.values() if s == "ok")
    warn_count = sum(1 for s in results.values() if s.startswith("warn:"))
    err_count = sum(1 for s in results.values() if s.startswith("error:"))
    print(
        f"\n=== 完成: {ok_count} ok / {warn_count} warn / {err_count} error (共 {len(items)}) ==="
    )
    print(f"文件位置: {run_dir}")

    if err_count > 0:
        failed = [iid for iid, s in results.items() if s.startswith("error:")]
        print(f"\n失败项: {', '.join(failed)}")
        print(
            f"重跑命令: python {sys.argv[0]} run --run-name {run_name} --item-id {' '.join(failed)}"
        )

    # upload + pack
    if not skip_upload:
        print("\n--- 上传到 Langfuse ---")
        if inline_item is not None:
            # GTM inline 模式：item 不在 Langfuse dataset，跳过 dataset 关联
            item = items[0]
            item_id = item["id"]
            item_ctx_override = {
                item_id: {
                    "item_id": item_id,
                    "user_message": item.get("user_message", ""),
                    "operation_type": item.get("operation_type", ""),
                    "difficulty": item.get("difficulty", ""),
                }
            }
            trace_map = batch_upload_sessions(
                run_dir,
                run_name,
                dataset_name,
                skill,
                None,
                description or f"GTM inline eval | {run_name}",
                skip_link=True,
                item_context_override=item_ctx_override,
            )
            for iid, tid in trace_map.items():
                print(f"STAGE: trace_uploaded trace_id={tid} item={iid}", flush=True)
        else:
            cmd_upload(
                run_name,
                dataset_name,
                item_ids,
                skill,
                model,
                model_full,
                description,
                skip_link=skip_link,
            )

    if not skip_pack:
        print("\n--- 打包 ---")
        cmd_pack(run_name)


# ── import-sessions 子命令 ────────────────────────────────────────────────────


def convert_history_to_jsonl(data: dict | list) -> str:
    """
    将 sessions_history API 返回值转换为 JSONL 格式（upload_session.py 兼容）。

    sessions_history 可能返回以下结构之一：
      - list[dict]              : 事件列表，每项直接是 otel event
      - {"events": [...], ...}  : 包含 events 字段的包装对象
      - {"session": {...}, "events": [...]} : 包含 session 元数据的包装对象

    输出：每行一个 JSON 事件，符合 upload_session.py 的 OpenClaw otel 格式。
    """
    events: list[dict] = []

    if isinstance(data, list):
        events = data
    elif isinstance(data, dict):
        if "events" in data:
            raw_events = data["events"]
            # 如有 session 元数据，作为第一个 session event 写入
            if "session" in data and isinstance(data["session"], dict):
                session_ev = {**data["session"], "type": "session"}
                events = [session_ev] + list(raw_events)
            else:
                events = list(raw_events)
        else:
            # 整个 dict 本身作为单个事件（兜底）
            events = [data]

    lines = [json.dumps(ev, ensure_ascii=False) for ev in events]
    return "\n".join(lines) + ("\n" if lines else "")


def cmd_import_sessions(
    run_name: str,
    dataset_name: str,
    item_ids: list[str] | None,
    export_dir: str | None,
) -> None:
    """
    从 wrapper subagent 写入的 /tmp/eval-sessions/{item_id}.json 导入到 run 目录。

    每个 JSON 文件是 sessions_history API 的原始返回值，本命令负责：
      1. 读取 JSON
      2. 转换为 JSONL（upload_session.py 兼容格式）
      3. 写入 run_dir/{item_id}.jsonl
    """
    items = get_dataset_items(dataset_name)
    if item_ids:
        items = [i for i in items if i["id"] in item_ids]

    src_dir = Path(export_dir) if export_dir else Path("/tmp/eval-sessions")
    run_dir = _RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 导入 session 文件 (run: {run_name}) ===")
    print(f"来源目录: {src_dir}\n")

    if not src_dir.exists():
        print(f"[ERROR] 来源目录不存在: {src_dir}")
        sys.exit(1)

    imported = 0
    missing = []

    for item in items:
        item_id = item["id"]
        src = src_dir / f"{item_id}.json"

        if not src.exists():
            print(f"  [{item_id}] MISSING  ({src})")
            missing.append(item_id)
            continue

        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  [{item_id}] ERROR  JSON 解析失败: {e}")
            missing.append(item_id)
            continue

        jsonl_content = convert_history_to_jsonl(raw)
        dst = run_dir / f"{item_id}.jsonl"
        dst.write_text(jsonl_content, encoding="utf-8")
        size_kb = dst.stat().st_size / 1024
        print(f"  [{item_id}] OK  ({size_kb:.0f} KB) -> {dst.name}")
        imported += 1

    print(f"\n导入完成: {imported}/{len(items)} 个 session")
    if missing:
        print(f"缺失: {', '.join(missing)}")
    print(f"文件位置: {run_dir}")

    manifest = {
        "run_name": run_name,
        "dataset_name": dataset_name,
        "source": "openclaw-wrapper",
        "imported_at": datetime.now(tz=timezone.utc).isoformat(),
        "items": {
            item["id"]: {
                "status": "imported" if item["id"] not in missing else "missing",
                "operation_type": item["operation_type"],
                "difficulty": item["difficulty"],
            }
            for item in items
        },
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


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
    """上传 session 到 Langfuse。

    Args:
        skip_link: True 时只上传 trace，不创建/关联 dataset run（适合调试少量 case）。
    """
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    # 自动构建 run_description（如未手动指定）
    run_description = description
    if not run_description:
        n_sessions = len(list(run_dir.glob("E2E-*.jsonl")))
        display_model = model_full or model
        run_description = (
            f"Openclaw 弱模型评测 | model: {display_model} | dataset: {dataset_name}"
            f" ({n_sessions} cases) | env: openclaw sandbox"
        )

    batch_upload_sessions(
        run_dir, run_name, dataset_name, skill, item_ids, run_description, skip_link=skip_link
    )


# ── pack 子命令 ────────────────────────────────────────────────────────────────


def _fetch_gce_metadata(path: str) -> str | None:
    """访问 GCE metadata server，拿不到时返回 None（非 GCE 环境/超时）。"""
    req = urllib.request.Request(f"{_METADATA_BASE}/{path}", headers=_METADATA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT) as resp:
            return resp.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _build_scp_command(archive: str) -> str:
    """拼接可直接复制粘贴的 gcloud scp 命令；非 GCE 环境退回占位符模板。"""
    # 以 zone 探测作为"是否在 GCE 上"的判据：metadata 拿到才信任 hostname 是实例名
    zone_full = _fetch_gce_metadata("instance/zone")  # 形如 projects/123/zones/asia-east2-b
    if zone_full is None:
        return (
            f"gcloud compute scp <实例名>:{archive} ~/Downloads/ "
            f"--zone=<zone> --project=<project-id>"
        )
    zone = zone_full.rsplit("/", 1)[-1]
    project = _fetch_gce_metadata("project/project-id") or "<project-id>"
    instance = socket.gethostname() or "<实例名>"
    return f"gcloud compute scp {instance}:{archive} ~/Downloads/ --zone={zone} --project={project}"


def cmd_pack(run_name: str) -> None:
    """打包 session 文件，方便下载到本地。"""
    run_dir = _RUNS_DIR / run_name
    if not run_dir.exists():
        print(f"[ERROR] Run 目录不存在: {run_dir}")
        sys.exit(1)

    archive = f"/tmp/eval-oc-{run_name}.tar.gz"
    subprocess.run(
        ["tar", "czf", archive, "-C", str(run_dir), "."],
        check=True,
    )

    size_mb = Path(archive).stat().st_size / 1024 / 1024
    print(f"打包完成: {archive} ({size_mb:.1f} MB)")
    print("\n下载到本地（在 Mac 终端执行）：")
    print(f"  {_build_scp_command(archive)}")


# ── dispatch 子命令（本地 Mac 端：并行调度多台 openclaw 服务器） ────────────────


def _parse_server_spec(spec: str) -> dict:
    """解析 server 规格 'name:zone:project' 为 dict。"""
    parts = spec.split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid server spec '{spec}', expected format 'name:zone:project'")
    return {"name": parts[0], "zone": parts[1], "project": parts[2]}


async def _ssh_exec(srv: dict, remote_cmd: str) -> tuple[str, str]:
    """SSH 到 server 执行一条 ubuntu 用户命令，返回 (stdout, stderr)。"""
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
        "--",
        f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode(), stderr.decode()


async def _setup_gateway_recipe_env(servers: list[dict]) -> None:
    """为每台服务器的 openclaw-gateway 注入 CAW_RECIPE_FILE / CAW_TELEMETRY=0 env。

    通过 systemd drop-in + restart 实现，agent 调 caw recipe search 时 caw 会自动
    读本地文件 `RECIPE_FILE_PATH`。初始化时写 empty-results 占位，避免文件缺失报错。
    """
    print("=== 为 openclaw-gateway 注入 CAW_RECIPE_FILE（systemd drop-in + restart）===")
    empty_recipe = json.dumps(
        {"message": "", "result": {"data": {"count": 0, "results": []}}},
        ensure_ascii=False,
    )
    setup_cmd = (
        f"echo {shlex.quote(empty_recipe)} > {RECIPE_FILE_PATH}; "
        "sudo mkdir -p /etc/systemd/system/openclaw-gateway.service.d; "
        "sudo tee /etc/systemd/system/openclaw-gateway.service.d/caw-eval-recipe.conf >/dev/null <<'EOF'\n"
        "[Service]\n"
        f"Environment=CAW_RECIPE_FILE={RECIPE_FILE_PATH}\n"
        "Environment=CAW_TELEMETRY=0\n"
        "EOF\n"
        "sudo systemctl daemon-reload && sudo systemctl restart openclaw-gateway && "
        "sleep 3 && echo setup-done"
    )
    results = await asyncio.gather(*(_ssh_exec(srv, setup_cmd) for srv in servers))
    for srv, (stdout, stderr) in zip(servers, results):
        if "setup-done" in stdout:
            print(f"  {srv['name']}: gateway env 注入完成")
        else:
            print(f"  {srv['name']}: 注入失败 — {stdout.strip()} {stderr.strip()}")
    print()


async def _teardown_gateway_recipe_env(servers: list[dict]) -> None:
    """恢复 openclaw-gateway 到原状态：删除 drop-in + restart。"""
    print("=== 清理 openclaw-gateway 的 CAW_RECIPE_FILE 注入 ===")
    teardown_cmd = (
        "sudo rm -f /etc/systemd/system/openclaw-gateway.service.d/caw-eval-recipe.conf && "
        "sudo systemctl daemon-reload && sudo systemctl restart openclaw-gateway && "
        f"rm -f {RECIPE_FILE_PATH} && sleep 2 && echo teardown-done"
    )
    results = await asyncio.gather(
        *(_ssh_exec(srv, teardown_cmd) for srv in servers), return_exceptions=True
    )
    for srv, result in zip(servers, results):
        if isinstance(result, Exception):
            print(f"  {srv['name']}: 清理异常 {result}")
        else:
            stdout, stderr = result
            if "teardown-done" in stdout:
                print(f"  {srv['name']}: 清理完成")
            else:
                print(f"  {srv['name']}: 清理失败 — {stdout.strip()} {stderr.strip()}")
    print()


def _build_remote_run_cmd(
    dataset_name: str,
    run_name: str,
    item_ids: list[str],
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    *,
    fire_and_forget: bool = False,
    server_name: str = "",
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> str:
    """构建要在远端 openclaw 服务器上执行的完整 shell 命令（传给 sudo su - ubuntu -c）。

    fire_and_forget=True 时：用 nohup 后台执行，SSH 在 echo 远端 PID 后立即返回。
    日志写到远端 ~/.caw-eval/runs/{run_name}/{server_name}.nohup.log。
    """
    item_args = " ".join(item_ids)
    # 远端 model-full 可能含 /，不会破坏 shell；但保险用 shlex.quote 包起来
    core_cmd = (
        "export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:$PATH; "
        "cd ~ && "
        "python3 -u ~/.agents/skills/caw-eval/scripts/run_eval_openclaw.py run "
        f"--run-name {shlex.quote(run_name)} "
        f"--dataset-name {shlex.quote(dataset_name)} "
        f"--item-id {item_args} "
        f"--timeout {timeout} "
        f"--skill {shlex.quote(skill)} "
        f"--model {shlex.quote(model)} "
        f"--model-full {shlex.quote(model_full)} "
        "--skip-pack"
    )
    if eval_mode != "standard":
        core_cmd += f" --eval-mode {shlex.quote(eval_mode)}"
    if recipe_mode:
        core_cmd += f" --recipe-mode {shlex.quote(recipe_mode)}"
    if not fire_and_forget:
        return core_cmd
    # fire-and-forget：nohup 后台运行，echo PID 后 SSH 立即返回
    # 本地日志文件（.log）只记录 PID 和 nohup log 路径；实际输出在远端 nohup log
    log_path = f"~/.caw-eval/runs/{run_name}/{server_name}.nohup.log"
    return (
        f"mkdir -p ~/.caw-eval/runs/{shlex.quote(run_name)}; "
        f"nohup bash -c {shlex.quote(core_cmd)} > {log_path} 2>&1 & echo $!"
    )


async def _ssh_dispatch_one(
    server: dict,
    item_ids: list[str],
    dataset_name: str,
    run_name: str,
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    log_dir: Path,
    *,
    fire_and_forget: bool = False,
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> tuple[str, int]:
    """SSH 到一台 server 串行执行其分配的 items，stdout/stderr 写入 log_dir/{name}.log。

    fire_and_forget=True 时：远端用 nohup 后台启动，SSH 在拿到 PID 后立即返回。
    本地日志只记录 PID + 远端 nohup log 路径，实际输出在远端。
    """
    if not item_ids:
        return server["name"], 0

    remote_cmd = _build_remote_run_cmd(
        dataset_name,
        run_name,
        item_ids,
        timeout,
        skill,
        model,
        model_full,
        fire_and_forget=fire_and_forget,
        server_name=server["name"],
        eval_mode=eval_mode,
        recipe_mode=recipe_mode,
    )
    ssh_cmd = [
        "gcloud",
        "compute",
        "ssh",
        "--zone",
        server["zone"],
        server["name"],
        "--tunnel-through-iap",
        "--project",
        server["project"],
        "--ssh-flag=-o ServerAliveInterval=60",
        "--ssh-flag=-o ServerAliveCountMax=10",
        "--",
        f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
    ]

    log_file = log_dir / f"{server['name']}.log"
    print(f"[DISPATCH→ {server['name']}] items={item_ids} log={log_file}")

    if fire_and_forget:
        # FF 模式：SSH 只等远端 echo PID，不等进程结束
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        pid = stdout.decode().strip()
        nohup_log = f"~/.caw-eval/runs/{run_name}/{server['name']}.nohup.log"
        with log_file.open("w", encoding="utf-8") as f:
            f.write("# fire-and-forget dispatch\n")
            f.write(f"# command: {' '.join(shlex.quote(c) for c in ssh_cmd)}\n")
            f.write(f"# remote PID: {pid}\n")
            f.write(f"# nohup log (on server): {nohup_log}\n")
            if stderr.strip():
                f.write(f"# stderr: {stderr.decode().strip()}\n")
        print(f"[DISPATCH← {server['name']}] fire-and-forget PID={pid}")
        print(f"  nohup log (on server): {nohup_log}")
        return server["name"], 0

    with log_file.open("w", encoding="utf-8") as f:
        f.write(f"# dispatch command:\n# {' '.join(shlex.quote(c) for c in ssh_cmd)}\n\n")
        f.flush()
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=f,
            stderr=asyncio.subprocess.STDOUT,
        )
        rc = await proc.wait()

    print(f"[DISPATCH← {server['name']}] rc={rc}")
    return server["name"], rc


async def _dynamic_worker(
    server: dict,
    queue: asyncio.Queue,
    item_results: dict,
    dataset_name: str,
    run_name: str,
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    log_dir: Path,
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> str:
    """动态 worker：从队列持续取 item 执行，直到队列空为止。

    每次只跑 1 个 item，完成后立即从队列取下一个，实现服务器间负载均衡。
    item_results[item_id] = (server_name, rc) 记录每个 item 的执行结果。
    """
    while True:
        try:
            item_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        remote_cmd = _build_remote_run_cmd(
            dataset_name,
            run_name,
            [item_id],
            timeout,
            skill,
            model,
            model_full,
            fire_and_forget=False,
            server_name=server["name"],
            eval_mode=eval_mode,
            recipe_mode=recipe_mode,
        )
        ssh_cmd = [
            "gcloud",
            "compute",
            "ssh",
            "--zone",
            server["zone"],
            server["name"],
            "--tunnel-through-iap",
            "--project",
            server["project"],
            "--ssh-flag=-o ServerAliveInterval=60",
            "--ssh-flag=-o ServerAliveCountMax=10",
            "--",
            f"sudo su - ubuntu -c {shlex.quote(remote_cmd)}",
        ]

        log_file = log_dir / f"{server['name']}-{item_id}.log"
        print(f"[DISPATCH→ {server['name']}] item={item_id} log={log_file.name}")

        with log_file.open("w", encoding="utf-8") as f:
            f.write(f"# dispatch command:\n# {' '.join(shlex.quote(c) for c in ssh_cmd)}\n\n")
            f.flush()
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=f,
                stderr=asyncio.subprocess.STDOUT,
            )
            # SSH 层硬超时：item_timeout + 60s 余量，防止 IAP tunnel / 远端僵尸拖死整个 dispatch
            ssh_timeout = timeout + 60
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=ssh_timeout)
            except asyncio.TimeoutError:
                f.write(f"\n# SSH timeout after {ssh_timeout}s, killing local ssh subprocess\n")
                f.flush()
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                rc = -1

        status = "OK" if rc == 0 else ("SSH_TIMEOUT" if rc == -1 else f"FAIL rc={rc}")
        print(f"[DISPATCH← {server['name']}] item={item_id} {status}")
        item_results[item_id] = (server["name"], rc)

        # 拉取服务器端归档的 pact spec（见 harness_pact_logger_bug.md）
        # 每个 item 跑完后服务器 `_archive_recent_pact_specs` 会写 pact_specs/<pact_id>.json
        # 评分端 score_traces.py --pact-specs-dir 会读这个目录
        if rc == 0:
            await _pull_pact_specs(server, run_name)

        queue.task_done()

    return server["name"]


async def _pull_pact_specs(server: dict, run_name: str) -> None:
    """从服务器拉回 pact_specs/ 目录。文件名是 pact_id，不同 item 归档不会冲突。

    用 `bash -c 'gcloud ssh ... | tar xzf -'` 让 shell 管理 pipe，asyncio 自身
    不承担 ssh_proc.stdout → tar_proc.stdin 的转接（asyncio.StreamReader 并非
    真实 fd，`stdin=ssh_proc.stdout` 会让 tar 立即收到 EOF 解出空包，之后
    `except Exception: pass` 根本没机会触发，pact_specs 永远是空目录）。
    """
    local_dir = _RUNS_DIR / run_name / "pact_specs"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = f"~/.caw-eval/runs/{run_name}/pact_specs"
    # 服务器端 tar 出 *.json；ls 门槛避免空目录时 tar 报错。
    remote_cmd = (
        f"sudo su - ubuntu -c 'cd {remote_dir} 2>/dev/null && "
        f"ls *.json >/dev/null 2>&1 && tar czf - *.json'"
    )
    ssh_argv = [
        "gcloud",
        "compute",
        "ssh",
        "--zone",
        server["zone"],
        "--project",
        server["project"],
        "--tunnel-through-iap",
        server["name"],
        "--",
        remote_cmd,
    ]
    # 把 ssh 命令交给 bash，让 shell 建真正的 fd pipe；tar 在 -C local_dir 解包。
    pipeline = (
        " ".join(shlex.quote(a) for a in ssh_argv)
        + f" | tar xzf - -C {shlex.quote(str(local_dir))}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            pipeline,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            # pipeline 失败不阻塞评测（score_traces 有 residual banner 兜底），
            # 但打印一行便于后续追查（空目录=假成功是最棘手的场景）
            err_tail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
            print(
                f"[WARN] pact_specs pull failed from {server['name']} rc={proc.returncode} {err_tail}"
            )
    except Exception as exc:
        print(f"[WARN] pact_specs pull exception from {server['name']}: {exc!r}")


async def _cmd_dispatch(
    dataset_name: str,
    run_name: str,
    item_ids: list[str] | None,
    servers: list[dict],
    timeout: int,
    skill: str,
    model: str,
    model_full: str,
    *,
    fire_and_forget: bool = False,
    static: bool = False,
    eval_mode: str = "standard",
    recipe_mode: str = "",
) -> None:
    """并行 dispatch 评测任务到多台 openclaw 服务器。

    默认动态队列模式（非 fire-and-forget）：所有 items 放入队列，每台服务器作为 worker
    持续取任务执行，完成一个立即取下一个，充分利用空闲服务器。

    fire_and_forget=True 或 static=True 时：退化为静态轮询分配（i % N），
    各台服务器预先分配固定 chunk，SSH 启动后（fire-and-forget 时）立即返回。
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

    n = len(servers)
    log_dir = _RUNS_DIR / run_name / "dispatch-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── CLI 健康预检：确保每台 openclaw agents add/delete 在 15s 内响应 ──────────
    # 背景：openclaw 没有 session GC，sessions.json 累积后 agents add 可能超 30s 静默挂
    #       （remote cmd_run 依旧 exit 0 → dispatch 误判为 OK）。启动前 smoke test，
    #       慢于 threshold 的服务器直接剔除，避免 item 分下去后才失败。
    #       修复方式：登录服务器跑 ~/.agents/skills/caw-eval/scripts/prune_openclaw_sessions.sh
    SMOKE_TIMEOUT_SEC = 15
    print("=== Openclaw CLI 健康预检（agents add/delete smoke）===")

    async def _smoke_check(srv: dict) -> tuple[dict, bool]:
        smoke_name = f"smoke-{int(datetime.now(timezone.utc).timestamp())}-{srv['name'][-8:]}"
        inner = (
            "export PATH=/home/ubuntu/.npm-global/bin:$PATH; "
            f"timeout {SMOKE_TIMEOUT_SEC} openclaw agents add {smoke_name} "
            "--workspace /home/ubuntu/.openclaw/workspace --non-interactive --json >/dev/null 2>&1 "
            f"&& timeout {SMOKE_TIMEOUT_SEC} openclaw agents delete {smoke_name} --force --json >/dev/null 2>&1 "
            "&& echo smoke-ok || echo smoke-fail"
        )
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
            "--",
            f"sudo su - ubuntu -c {shlex.quote(inner)}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SMOKE_TIMEOUT_SEC * 3 + 10
            )
        except asyncio.TimeoutError:
            proc.kill()
            return srv, False
        return srv, "smoke-ok" in stdout.decode()

    smoke_results = await asyncio.gather(*(_smoke_check(s) for s in servers))
    healthy_servers: list[dict] = []
    for srv, ok in smoke_results:
        if ok:
            print(f"  {srv['name']}: OK")
            healthy_servers.append(srv)
        else:
            print(
                f"  {srv['name']}: FAIL — 跳过（agents add/delete 超 {SMOKE_TIMEOUT_SEC}s；"
                f"SSH 上去跑 sudo ~/.agents/skills/caw-eval/scripts/prune_openclaw_sessions.sh 清 sessions.json）"
            )
    if not healthy_servers:
        print("[ABORT] 所有服务器 CLI 健康预检失败，无法分发")
        sys.exit(2)
    if len(healthy_servers) < len(servers):
        print(
            f"[WARN] 跳过 {len(servers) - len(healthy_servers)} 台，"
            f"继续用剩下 {len(healthy_servers)} 台跑评测\n"
        )
    servers = healthy_servers
    n = len(servers)
    print()

    # ── 预清理：并行 SSH 到各服务器，删除所有历史残留 eval agent + session 目录 ──
    print("=== 预清理历史残留 eval agent / session 目录 ===")
    cleanup_tasks = []
    for srv in servers:
        cleanup_cmd = (
            "export PATH=/home/ubuntu/.npm-global/bin:$PATH; "
            # 列出所有 eval- 开头的 agent 并逐个 --force 删除
            "for a in $(openclaw agents list 2>&1 "
            "| awk '/^- eval-/{print $2}' ); do "
            '  openclaw agents delete "$a" --force 2>&1; '
            "done; "
            # 清理残留 session 目录（仅删 ~/.openclaw/agents/ 下 eval- 开头的一级目录）
            "find ~/.openclaw/agents -maxdepth 1 -type d -name 'eval-*' -exec rm -rf {} + 2>/dev/null; "
            "echo cleanup-done"
        )
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
            "--",
            f"sudo su - ubuntu -c {shlex.quote(cleanup_cmd)}",
        ]
        cleanup_tasks.append(
            asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        )
    cleanup_procs = await asyncio.gather(*cleanup_tasks)
    cleanup_results = await asyncio.gather(*(p.communicate() for p in cleanup_procs))
    for srv, (stdout, stderr) in zip(servers, cleanup_results):
        out = stdout.decode().strip()
        if "cleanup-done" in out:
            print(f"  {srv['name']}: 清理完成")
        else:
            print(f"  {srv['name']}: 清理可能不完整 — {out} {stderr.decode().strip()}")
    print()

    # ── Recipe 模式：为每台服务器的 openclaw-gateway 注入 CAW_RECIPE_FILE env ──
    # 通过 systemd drop-in 让 gateway 进程持有 env var，caw recipe search 自动读本地文件。
    # 初始化时写入 empty-results 占位，避免文件缺失报错；每个 item 开始前 _run_single_task
    # 会覆写为实际 recipe 内容。
    recipe_gateway_active = eval_mode == "recipe" and recipe_mode == "openclaw"
    if recipe_gateway_active:
        await _setup_gateway_recipe_env(servers)

    try:
        # ── 静态分配路径（fire-and-forget 或显式 --static）────────────────────────
        if fire_and_forget or static:
            chunks: list[list[str]] = [[] for _ in range(n)]
            for i, item in enumerate(items):
                chunks[i % n].append(item["id"])

            mode = "fire-and-forget" if fire_and_forget else "static"
            print(f"=== Dispatch [{mode}] (run: {run_name}) ===")
            print(f"数据集: {dataset_name} ({len(items)} items)")
            print(f"服务器: {n}, 模型: {model_full or model}")
            for srv, chunk in zip(servers, chunks):
                print(f"  → {srv['name']} [{srv['zone']}]: {chunk}")
            print(f"日志目录: {log_dir}")
            print()

            coroutines = [
                _ssh_dispatch_one(
                    srv,
                    chunk,
                    dataset_name,
                    run_name,
                    timeout,
                    skill,
                    model,
                    model_full,
                    log_dir,
                    fire_and_forget=fire_and_forget,
                    eval_mode=eval_mode,
                    recipe_mode=recipe_mode,
                )
                for srv, chunk in zip(servers, chunks)
            ]
            static_results = await asyncio.gather(*coroutines, return_exceptions=True)

            print("\n=== 完成 ===")
            failures: list[str] = []
            for srv, result in zip(servers, static_results):
                if isinstance(result, Exception):
                    print(f"  {srv['name']}: EXCEPTION {result}")
                    failures.append(srv["name"])
                else:
                    _, rc = result  # type: ignore[misc]
                    status = "OK" if rc == 0 else f"FAIL rc={rc}"
                    print(f"  {srv['name']}: {status}")
                    if rc != 0:
                        failures.append(srv["name"])

            if failures:
                print(f"\n失败服务器: {failures}")
                print(f"查看日志: {log_dir}/<server>.log")
                if fire_and_forget:
                    print(
                        f"或查看 nohup log：ssh 到各服务器看 ~/.caw-eval/runs/{run_name}/<server>.nohup.log"
                    )
            else:
                print(f"\n所有 server 执行完毕。Langfuse run: {run_name}")
                print(
                    "下一步：参考 references/run-eval-openclaw.md Step 3-4 评分（score_traces.py langfuse）"
                )
            return

        # ── 动态队列路径（默认）────────────────────────────────────────────────────
        print(f"=== Dispatch [dynamic] (run: {run_name}) ===")
        print(f"数据集: {dataset_name} ({len(items)} items)")
        print(f"服务器: {n} workers, 模型: {model_full or model}")
        print("模式: 动态队列（空闲服务器自动取下一个任务）")
        all_ids = [item["id"] for item in items]
        print(f"任务队列: {all_ids}")
        print(f"日志目录: {log_dir}")
        print()

        queue: asyncio.Queue = asyncio.Queue()
        for item in items:
            await queue.put(item["id"])

        item_results: dict[str, tuple[str, int]] = {}

        workers = [
            _dynamic_worker(
                srv,
                queue,
                item_results,
                dataset_name,
                run_name,
                timeout,
                skill,
                model,
                model_full,
                log_dir,
                eval_mode=eval_mode,
                recipe_mode=recipe_mode,
            )
            for srv in servers
        ]
        await asyncio.gather(*workers)

        print("\n=== 完成 ===")
        failed_items: list[str] = []
        for item_id, (srv_name, rc) in sorted(item_results.items()):
            status = "OK" if rc == 0 else f"FAIL rc={rc}"
            print(f"  [{srv_name}] {item_id}: {status}")
            if rc != 0:
                failed_items.append(item_id)

        if failed_items:
            print(f"\n失败 items: {failed_items}")
            print(f"查看日志: {log_dir}/<server>-<item_id>.log")
            print(f"重跑命令示例: --item-id {' '.join(failed_items)}")
            sys.exit(1)
        else:
            print(f"\n所有 {len(item_results)} 个 item 执行完毕。Langfuse run: {run_name}")
            print(
                "下一步：参考 references/run-eval-openclaw.md Step 3-4 评分（score_traces.py langfuse）"
            )
    finally:
        # fire-and-forget 模式下 SSH 立即返回但远端还在跑，此时 teardown 会过早；
        # 仅在阻塞模式下（dispatch 全部完成后）才清理 gateway env。
        if recipe_gateway_active and not fire_and_forget:
            await _teardown_gateway_recipe_env(servers)


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Openclaw 弱模型评测脚本（三层分离方案的服务器端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    # ── run（推荐）
    p_run = sub.add_parser("run", help="脚本驱动串行执行评测（推荐）")
    p_run.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_run.add_argument("--run-name", required=True)
    p_run.add_argument("--item-id", nargs="*", help="只执行指定 item")
    p_run.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT, help="单个 task 超时秒数")
    p_run.add_argument("--openclaw-bin", default="openclaw", help="openclaw 二进制路径")
    p_run.add_argument(
        "--workspace",
        default=str(_OC_HOME / "workspace"),
        help="Openclaw workspace 路径（默认 ~/.openclaw/workspace）",
    )
    p_run.add_argument("--skip-upload", action="store_true", help="跳过上传 Langfuse")
    p_run.add_argument("--skip-pack", action="store_true", help="跳过打包")
    p_run.add_argument(
        "--no-link",
        action="store_true",
        help="只上传 trace，不创建/关联 dataset run（指定少量 case 调试时用）",
    )
    p_run.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_run.add_argument("--model", default="doubao", help="模型短标识")
    p_run.add_argument("--model-full", default="", help="完整模型 ID")
    p_run.add_argument("--description", default="", help="自定义 run description")
    p_run.add_argument(
        "--eval-mode",
        choices=["standard", "recipe"],
        default="standard",
        help="评测模式: standard（默认）或 recipe（交易构建模式）",
    )
    p_run.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "openclaw"],
        default="",
        help="Recipe 对比模式（仅 --eval-mode recipe 时有效）",
    )
    p_run.add_argument(
        "--inline-item",
        default=None,
        help="GTM 模式：直接传入 item JSON 字符串，跳过 Langfuse dataset 拉取。"
        ' 格式：\'{"id":"...","user_message":"...","operation_type":"...",'
        '"difficulty":"...","metadata":{...},"expected_output":{...}}\'',
    )

    # ── import-sessions
    p_import = sub.add_parser("import-sessions", help="从外部导出的 JSON 导入 session")
    p_import.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_import.add_argument("--run-name", required=True)
    p_import.add_argument("--item-id", nargs="*", help="只导入指定 item")
    p_import.add_argument("--export-dir", help="session 导出目录（默认 /tmp/eval-sessions）")

    # ── upload
    p_upload = sub.add_parser("upload", help="上传 session 到 Langfuse")
    p_upload.add_argument("--run-name", required=True)
    p_upload.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_upload.add_argument("--item-id", nargs="*", help="只上传指定 item")
    p_upload.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_upload.add_argument("--model", default="ark-code", help="模型短标识（用于 run description）")
    p_upload.add_argument(
        "--model-full", default="ark-code-latest", help="完整模型 ID，写入 run description"
    )
    p_upload.add_argument(
        "--description", default="", help="自定义 run description（覆盖自动生成）"
    )
    p_upload.add_argument(
        "--no-link",
        action="store_true",
        help="只上传 trace，不创建/关联 dataset run",
    )

    # ── pack
    p_pack = sub.add_parser("pack", help="打包 session 文件供下载")
    p_pack.add_argument("--run-name", required=True)

    # ── dispatch（本地 Mac 端：并行调度 N 台 openclaw 服务器）
    p_dispatch = sub.add_parser(
        "dispatch",
        help="本地 Mac 端：并行 SSH 到多台 openclaw 服务器，每台串行执行其分配的 items",
    )
    p_dispatch.add_argument("--dataset-name", default="caw-agent-eval-seth-v2")
    p_dispatch.add_argument("--run-name", required=True)
    p_dispatch.add_argument("--item-id", nargs="*", help="只分发指定 item（否则使用整个 dataset）")
    p_dispatch.add_argument(
        "--server",
        action="append",
        required=True,
        metavar="name:zone:project",
        help="gcloud 服务器规格，可重复；items 轮询分配（i %% N）到各台",
    )
    p_dispatch.add_argument(
        "--timeout", type=int, default=_DEFAULT_TIMEOUT, help="远端单 task 超时（秒）"
    )
    p_dispatch.add_argument("--skill", default="cobo-agentic-wallet-sandbox")
    p_dispatch.add_argument("--model", required=True, help="模型短标识，如 doubao")
    p_dispatch.add_argument(
        "--model-full", default="", help="完整模型 ID，如 volcengine/doubao-seed-2.0-code"
    )
    p_dispatch.add_argument(
        "--static",
        action="store_true",
        help=(
            "静态轮询分配模式（i %% N）：items 预先固定分给每台服务器，不做动态调度。"
            "默认为动态队列模式（空闲服务器自动取下一个任务）。"
            "fire-and-forget 时自动启用静态模式。"
        ),
    )
    p_dispatch.add_argument(
        "--fire-and-forget",
        action="store_true",
        help=(
            "后台模式：SSH 启动远端 nohup 进程后立即返回，不等待评测完成。"
            "进度通过 score_traces.py langfuse --watch 轮询 Langfuse 跟踪。"
            "隐含 --static（后台模式无法动态调度）。"
        ),
    )
    p_dispatch.add_argument(
        "--eval-mode",
        choices=["standard", "recipe"],
        default="standard",
        help="评测模式: standard（默认）或 recipe（交易构建模式）",
    )
    p_dispatch.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "openclaw"],
        default="",
        help="Recipe 对比模式（仅 --eval-mode recipe 时有效）",
    )

    args = parser.parse_args()

    if args.cmd == "run":
        asyncio.run(
            _cmd_run(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                timeout=args.timeout,
                openclaw_bin=args.openclaw_bin,
                workspace=args.workspace,
                skip_upload=args.skip_upload,
                skip_pack=args.skip_pack,
                skill=args.skill,
                model=args.model,
                model_full=args.model_full,
                description=args.description,
                skip_link=args.no_link,
                eval_mode=args.eval_mode,
                recipe_mode=args.recipe_mode,
                inline_item=args.inline_item,
            )
        )
    elif args.cmd == "import-sessions":
        cmd_import_sessions(
            run_name=args.run_name,
            dataset_name=args.dataset_name,
            item_ids=args.item_id,
            export_dir=args.export_dir,
        )
    elif args.cmd == "upload":
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
    elif args.cmd == "pack":
        cmd_pack(run_name=args.run_name)
    elif args.cmd == "dispatch":
        servers = [_parse_server_spec(s) for s in args.server]
        asyncio.run(
            _cmd_dispatch(
                dataset_name=args.dataset_name,
                run_name=args.run_name,
                item_ids=args.item_id,
                servers=servers,
                timeout=args.timeout,
                skill=args.skill,
                model=args.model,
                model_full=args.model_full,
                fire_and_forget=args.fire_and_forget,
                static=args.static,
                eval_mode=args.eval_mode,
                recipe_mode=args.recipe_mode,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
