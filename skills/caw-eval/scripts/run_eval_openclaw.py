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
      --dataset-name standard-test-v3 \\
      --model doubao --model-full volcengine/doubao-seed-2.0-code \\
      --server srv1:asia-east2-a:my-project \\
      --server srv2:asia-east2-c:my-project \\
      --server srv3:asia-east2-c:my-project

单台服务器直接 run（dispatch 内部也调这个）:
    python run_eval_openclaw.py run \\
      --run-name eval-oc-doubao-20260415 \\
      --dataset-name standard-test-v3
"""

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eval_utils import (
    _normalize_eval_mode,
    _normalize_recipe_source,
    batch_upload_sessions,
    get_dataset_items,
    get_langfuse_client,
    link_to_dataset_run,
    print_dataset_summary,
    resolve_dataset,
    upload_session,
)

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

# dispatch 启动前的 caw 健康预检：低于此 TSS Node 版本直接 abort，避免 caw 在 sign/tx
# 阶段触发 ensureRuntimeTSSNodeMinVersion 二进制热升级 → ETXTBSY (text file busy)。
# 默认值 v0.12.14 对齐 caw SDK defaultMinVersion (sdk/go/internal/tssnode/version.go:13)。
# 服务端可通过 X-CAW-TSS-Node-Min-Version 推更高 min（例如 v0.12.20），可由
# CAW_EVAL_PREFLIGHT_MIN_TSS_VERSION 环境变量在本地预检阶段对齐这一更高基线。
_DEFAULT_PREFLIGHT_MIN_TSS_VERSION = "v0.12.14"

# ── 余额 gate 阈值（Base 主网）──────────────────────────────────────────────────
# 单 case 最坏负担：transfer/swap/supply ≤ 0.002 USDC，wrap ≤ 0.0001 ETH，
# superfluid upgrade 易把可用 USDC 一次性 wrap → USDCx（实测 minimax 04-28 把 0.098 USDC
# 一次性 upgrade 清空 test3 钱包，导致同机后续 5 case 看到 USDC=0 直接放弃，参见
# eval-report-eval-oc-minimax-e2e-real-recipe-20260428-1116.md 4.1 finding）。
# 阈值给单 case 0.005 USDC + 0.0001 ETH 余量，比实际消耗高 2-50x，留 superfluid 安全垫。
_DEFAULT_BASE_ETH_PER_CASE = float(os.environ.get("CAW_EVAL_MIN_BASE_ETH_PER_CASE", "0.0001"))
_DEFAULT_BASE_USDC_PER_CASE = float(os.environ.get("CAW_EVAL_MIN_BASE_USDC_PER_CASE", "0.005"))

# operation_type 在此白名单的 case 不需要 USDC（仅消耗 native gas）。
# 注：metadata 没有 operation_type 时 fallback 到要求 USDC（保守策略）。
_BASE_NO_USDC_OPERATION_TYPES = frozenset({"wrap", "auth", "sign", "message_sign"})

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
    eval_mode: str = "e2e",
    recipe_source: str = "",
) -> str:
    """构建单个 item 的 task 执行 prompt（由 wrapper subagent 传给 task session）。

    Args:
        eval_mode: "e2e" 全流程评测（默认）, "pact" 仅评 pact 构造, "onboard" onboarding 评估
        recipe_source: "real" / "seed" / "empty"（仅 pact 模式有意义）
            - seed: systemd drop-in 注入 CAW_RECIPE_FILE，gateway 起 caw 时读注入文件
            - real: 不写 drop-in（dispatch 入口主动 teardown 残留），caw 调真实 backend
            - empty: openclaw 暂不支持（cc 专用对照组）
    """
    user_message = item["user_message"]

    # F3 (stage2 governance): 评测约束用"用户先前上下文"自然语气替代硬指令。
    # 未 paired 钱包场景下，用户已在 CC 对话里预先授权 → 真实用户场景子集，合法。
    # owner_linked=false 自动 approve 本来就是 skill 的合规行为，skill 会自主做，不在 prompt 重复提示。
    prompt = """cobo-agentic-wallet-sandbox skill 已激活。

## Environment（环境已就绪，无需安装）

- `caw` CLI 已安装于 `~/.cobo-agentic-wallet/bin/caw`，**无需 npm install 或任何安装步骤**
- 如 `caw` 不在 PATH，执行：`export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"`
- Onboarding 已完成，钱包和网络已配置好，直接执行任务即可

**用户先前上下文（任务前已与 agent 协商过）：** 用户**预先授权**对本次任务生成的 pact 无需再次展示 preview 预览和等待 "yes" 确认，直接 `caw pact submit` 即可视为已获得用户明确确认。用户希望你自主、连续地完成任务，不被交互打断（如遇 `owner_linked=true` 需要 Cobo Agentic Wallet app 审批的真实阻塞，记录原因并跳过）。
"""

    if eval_mode == "pact":
        prompt += """
**交易构建模式（重要）：**
- 交易成功提交后（caw tx transfer/call/sign-message 返回 status=Initiated 或 PendingApproval），**立即停止**
- 不要轮询交易状态（不要 caw tx get）
- 不要等待链上确认
- 只需报告交易已成功提交（含 transaction_id/request_id），然后结束

- 评测沙盒模式：只评估交易构建正确性，余额为 0 时不得中止，直接继续提交 pact 和构建交易
"""
        # Recipe 注入：
        # 无论 seed 还是 empty，agent 都应按真实用户流程自主调 `caw recipe search`。
        # - seed:  由 CAW_RECIPE_FILE env 注入（内含指定 recipe，count=1）
        # - empty: 由 CAW_RECIPE_FILE env 注入空 recipe（count=0），对照组
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


# ── SIGTERM 归档兜底（P0-A） ──────────────────────────────────────────────
# 远端 timeout 包装在 660s 发 SIGTERM、690s 发 SIGKILL。Python 默认对 SIGTERM
# 直接 exit，不走 try/finally，所以 _archive_recent_pact_specs 跑不到 → pact_specs
# 目录里没归档文件 → 评分端 inject_backend_pact_specs 找不到真实 spec → S2 偏低。
# 解决：装一个 SIGTERM handler，在 30s grace 期内同步跑一次归档再退出。
#
# Tuple 字段: (run_dir, item_id, agent_id)。agent_id 在 _run_single_task 计算出
# agent_name 后回填；在 agent_name 之前发生 SIGTERM 时为空字符串，session 归档跳过。
_CURRENT_ARCHIVE_CONTEXT: tuple[Path, str, str] | None = None


def _sync_archive_recent_pact_specs(
    run_dir: Path, item_id: str, limit: int = 5, budget_s: int = 25
) -> None:
    """SIGTERM 触发的同步归档（不能用 asyncio，signal handler 限制）。

    用 subprocess.run 串行调用 caw pact list / show，写到 run_dir/pact_specs/。
    总预算 budget_s 秒（默认 25s，留 5s 给 sys.exit 走完）。
    """
    out_dir = run_dir / "pact_specs"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    deadline = time.monotonic() + budget_s
    try:
        result = subprocess.run(
            [_CAW_BIN, "pact", "list", "--limit", str(limit)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return
        listing = json.loads(result.stdout.decode())
        pacts = listing.get("result", {}).get("pacts", []) or []
    except Exception:
        return

    archived = 0
    for p in pacts:
        if time.monotonic() > deadline:
            break
        pid = p.get("id", "")
        if not pid:
            continue
        dst = out_dir / f"{pid}.json"
        if dst.exists():
            continue
        try:
            r = subprocess.run(
                [_CAW_BIN, "pact", "show", "--pact-id", pid],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if r.returncode != 0:
                continue
            dst.write_bytes(r.stdout)
            archived += 1
        except Exception:
            continue

    if archived:
        # 用 stderr 避免被 stdout 缓冲影响
        sys.stderr.write(
            f"  [{item_id}] sigterm-archived {archived} pact spec(s) -> {out_dir.name}/\n"
        )
        sys.stderr.flush()


def _sync_archive_session(run_dir: Path, item_id: str, agent_id: str) -> None:
    """SIGTERM 时把当前 agent 的 session jsonl 拷到 run_dir，加 .partial 后缀。

    `.partial` 后缀避免被 `STAGE: session_collected` 流程误认成完整 session（那个流程
    glob `*.jsonl` 拿最新，不区分前缀；但下游 `dispatch_pull_raw_sessions` 用
    `ls *.jsonl` 会一并拉回本地 raw-sessions/，judge 评分可识别后缀单独处理）。

    成本：单次 shutil.copy2 几百 KB，约 10-50 ms，远低于 SIGTERM 30s grace 预算。
    """
    if not agent_id:
        sys.stderr.write(
            f"  [{item_id}] sigterm-skip session: agent_id 未知（agent_name 计算前就 SIGTERM）\n"
        )
        return
    session_dir = _OC_HOME / "agents" / agent_id / "sessions"
    if not session_dir.exists():
        truncated = agent_id[:64]
        candidate = _OC_HOME / "agents" / truncated / "sessions"
        if candidate.exists():
            session_dir = candidate
        else:
            sys.stderr.write(f"  [{item_id}] sigterm-skip session: dir 不存在 ({session_dir})\n")
            return
    files = sorted(
        (f for f in session_dir.glob("*.jsonl") if f.name != "sessions.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        sys.stderr.write(f"  [{item_id}] sigterm-skip session: 无 jsonl 文件\n")
        return
    dst = run_dir / f"{item_id}.partial.jsonl"
    try:
        shutil.copy2(files[0], dst)
        size_kb = dst.stat().st_size / 1024
        sys.stderr.write(
            f"  [{item_id}] sigterm-archived session ({size_kb:.0f}KB) -> {dst.name}\n"
        )
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"  [{item_id}] sigterm-archive session failed: {e}\n")


def _sigterm_archive_handler(signum, frame):  # noqa: ARG001
    """SIGTERM/SIGINT 时同步归档当前 item 再退出。

    远端 `timeout --signal=TERM --kill-after=30s` 给我们 ~30s 窗口。
    归档 5 个 pact (caw pact show) 大约 10-15s，session copy 几十 ms，足够。
    """
    ctx = _CURRENT_ARCHIVE_CONTEXT
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    sys.stderr.write(f"\n[{sig_name}] caught, archiving current item before exit...\n")
    sys.stderr.flush()
    if ctx is not None:
        run_dir, item_id, agent_id = ctx
        try:
            _sync_archive_session(run_dir, item_id, agent_id)
        except Exception as e:
            sys.stderr.write(f"[{sig_name}] session archive failed: {e}\n")
        try:
            _sync_archive_recent_pact_specs(run_dir, item_id)
        except Exception as e:
            sys.stderr.write(f"[{sig_name}] pact archive failed: {e}\n")
    # 标准 SIGTERM exit code = 128 + signum
    sys.exit(128 + signum)


def _install_sigterm_archive_handler() -> None:
    """在 cmd_run 入口装上 handler。多次装等同最后一次（signal 模块保证）。"""
    signal.signal(signal.SIGTERM, _sigterm_archive_handler)
    signal.signal(signal.SIGINT, _sigterm_archive_handler)


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
    eval_mode: str = "e2e",
    recipe_source: str = "",
) -> str:
    """执行单个评测 task，返回状态字符串 ("ok" | "error:<reason>")。"""
    item_id = item["id"]
    # hash 化 agent_name（固定 13 字符，永不破 openclaw 64 字符限制）。
    # 历史拼接 f"eval-{item_id}-{run_dir.name}" 可达 80+ 字符，被 openclaw 截断到 64
    # 导致 session_dir 找不到（参 plan 10-recipe-3 P0 评测工具链 Bug 修复）。
    # 同 (item_id, run_name) 永远 hash 到同 short_id，重跑幂等。
    short_id = hashlib.sha1(f"{run_dir.name}/{item_id}".encode()).hexdigest()[:8]
    agent_name = f"eval-{short_id}"
    actual_agent_id = ""

    # 回填 SIGTERM context 的 agent_id（用 agent_name.lower() 提前给出，actual_agent_id
    # 在 `agents add` 返回后才确定，但 openclaw 仅做 lowercase 化，路径是 agent_name.lower()）。
    # 这样若 SIGTERM 在 agents add 之后、session_collected 之前命中，session 仍能归档到
    # run_dir/<item>.partial.jsonl。
    global _CURRENT_ARCHIVE_CONTEXT
    if _CURRENT_ARCHIVE_CONTEXT is not None:
        _CURRENT_ARCHIVE_CONTEXT = (run_dir, item_id, agent_name.lower())

    # 写 mapping 到 run_dir/agent_map.jsonl（追加；多 case 并发用 jsonl 不冲突）
    mapping_entry = {
        "short_id": short_id,
        "agent_name": agent_name,
        "item_id": item_id,
        "run_name": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    mapping_file = run_dir / "agent_map.jsonl"
    mapping_file.parent.mkdir(parents=True, exist_ok=True)
    with open(mapping_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(mapping_entry, ensure_ascii=False) + "\n")

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
        if eval_mode == "pact" and recipe_source == "seed" and recipe_content:
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

        prompt = build_task_prompt(item, eval_mode=eval_mode, recipe_source=recipe_source)
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
        # 新 run（hash 化 agent_name，13 字符）→ session_dir 直接命中。
        # 兜底：老 run 用拼接长名跑过的 session，agent_name 在磁盘被截到 64 字符——
        # 用 truncated 路径 fallback 找到（参 plan 10-recipe-3 方案 A 兜底）。
        session_dir = _OC_HOME / "agents" / actual_agent_id / "sessions"
        if not session_dir.exists():
            truncated = actual_agent_id[:64]
            candidate = _OC_HOME / "agents" / truncated / "sessions"
            if candidate.exists():
                session_dir = candidate
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
    eval_mode: str = "e2e",
    recipe_source: str = "",
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

    # P0-A: 装 SIGTERM/SIGINT handler，远端 timeout 包装发 SIGTERM 时同步归档当前 item
    # 的 pact spec，避免 SIGKILL 强杀前归档代码跑不到（详见 _sigterm_archive_handler 注释）
    _install_sigterm_archive_handler()

    results: dict[str, str] = {}

    global _CURRENT_ARCHIVE_CONTEXT
    for i, item in enumerate(items):
        item_id = item["id"]
        op = item["operation_type"]
        diff = item["difficulty"]
        print(f"[{i + 1}/{len(items)}] {item_id} ({op} {diff})")
        # 设置当前 item 的归档上下文，SIGTERM handler 会读这个变量决定归档目标
        # agent_id 此时未知（要等 _run_single_task 算完 hash），先填空字符串占位；
        # _run_single_task 内部会在 agent_name 算出后回填 _CURRENT_ARCHIVE_CONTEXT。
        _CURRENT_ARCHIVE_CONTEXT = (run_dir, item_id, "")
        try:
            status = await _run_single_task(
                item,
                openclaw_bin,
                workspace,
                run_dir,
                timeout,
                eval_mode=eval_mode,
                recipe_source=recipe_source,
            )
            results[item_id] = status
        finally:
            # item 完成（无论成功/失败/异常），清掉上下文，避免误归档下个 item
            _CURRENT_ARCHIVE_CONTEXT = None

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
    eval_mode: str = "e2e",
    recipe_source: str = "",
) -> str:
    """构建要在远端 openclaw 服务器上执行的完整 shell 命令（传给 sudo su - ubuntu -c）。

    fire_and_forget=True 时：用 nohup 后台执行，SSH 在 echo 远端 PID 后立即返回。
    日志写到远端 ~/.caw-eval/runs/{run_name}/{server_name}.nohup.log。
    """
    item_args = " ".join(item_ids)
    # 远端外层硬超时：timeout + 60s（与本地 ssh_timeout 对齐）。
    # 作用：哪怕本地 SSH 被 kill，远端 python3 进程到点也会被自己 SIGTERM/SIGKILL，
    # 不再形成"孤儿进程被新 SSH 撞上"的并发污染。
    # --kill-after=30s：发完 SIGTERM 再等 30s 仍不退就 SIGKILL（兜底）。
    outer_timeout = timeout + 60
    # 远端 model-full 可能含 /，不会破坏 shell；但保险用 shlex.quote 包起来
    core_cmd = (
        "export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:$PATH; "
        "cd ~ && "
        f"timeout --signal=TERM --kill-after=30s {outer_timeout}s "
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
    if eval_mode != "e2e":
        core_cmd += f" --eval-mode {shlex.quote(eval_mode)}"
    if recipe_source:
        core_cmd += f" --recipe-source {shlex.quote(recipe_source)}"
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
    eval_mode: str = "e2e",
    recipe_source: str = "",
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
        recipe_source=recipe_source,
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


def _case_chain(item: dict) -> str:
    """从 dataset item.metadata 取规范化 chain 名（小写）。"""
    return (item.get("metadata", {}).get("chain") or "").lower()


def _case_needs_token(item: dict, token: str) -> bool:
    """推断 case 是否需要某 token：基于 metadata.operation_type 白名单。

    metadata 缺 operation_type 时按"需要"处理（保守，避免漏 gate 让 wallet 真耗尽）。
    """
    op = (item.get("metadata", {}).get("operation_type") or "").lower()
    if token == "BASE_ETH_USDC":
        return op not in _BASE_NO_USDC_OPERATION_TYPES
    return True  # native gas 所有 mainnet case 都需要


async def _query_remote_balance(server: dict) -> dict | None:
    """SSH 跑 ``caw wallet balance``，解析返回 ``{(chain_id, token_id): amount_float}``。

    SSH 失败 / JSON 解析失败 → 返回 None；调用方按 "skip-gate (放行)" 处理，
    避免临时网络抖动误杀 case。
    """
    inner = (
        "export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:$PATH; "
        "caw wallet balance 2>&1"
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
        "--",
        f"sudo su - ubuntu -c {shlex.quote(inner)}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        text = stdout.decode("utf-8", "replace")
        idx = text.find("{")
        if idx < 0:
            return None
        d = json.loads(text[idx:])
        out: dict[tuple[str, str], float] = {}
        for r in d.get("result", []):
            chain = r.get("chain_id") or ""
            tok = r.get("token_id") or ""
            try:
                amt = float(r.get("amount") or "0")
            except (TypeError, ValueError):
                amt = 0.0
            out[(chain, tok)] = amt
        return out
    except Exception:
        return None


def _balance_gate_check_one_case(item: dict, balances: dict) -> tuple[bool, str]:
    """对单个 mainnet case 检查目标 server 当前余额是否足够。

    返回 ``(ok, msg)``。non-mainnet (chain ∉ {base, polygon}) 视为 OK 直接放行。
    """
    chain = _case_chain(item)
    if chain == "base":
        eth = balances.get(("BASE_ETH", "BASE_ETH"), 0.0)
        if eth < _DEFAULT_BASE_ETH_PER_CASE:
            return False, f"BASE_ETH={eth:.6f} < {_DEFAULT_BASE_ETH_PER_CASE}"
        if _case_needs_token(item, "BASE_ETH_USDC"):
            usdc = balances.get(("BASE_ETH", "BASE_ETH_USDC"), 0.0)
            if usdc < _DEFAULT_BASE_USDC_PER_CASE:
                return False, (f"BASE_ETH_USDC={usdc:.4f} < {_DEFAULT_BASE_USDC_PER_CASE}")
            return True, f"BASE_ETH={eth:.4f} USDC={usdc:.4f}"
        return True, f"BASE_ETH={eth:.4f} (no USDC needed)"
    return True, f"skip-gate(chain={chain})"


async def _dispatch_balance_preflight(
    items: list[dict],
    servers: list[dict],
    *,
    safety: float = 1.5,
    abort_on_fail: bool = False,
) -> None:
    """启动时一次性预检：单机最坏负担全部 mainnet case 时的余额需求。

    动态队列下负载不均，慢机分到的 case 少；单机最坏可能跑全部 mainnet case。
    所以按"单机 = sum(case_count) × per_case × safety"算阈值，比传统"总量 / N 机"
    严，避免某台慢机拖到中途余额耗尽。

    abort_on_fail=True 时余额不足直接 sys.exit(2)；默认 warn 不阻塞（保留用户决定权）。
    """
    base_eth_count = sum(1 for it in items if _case_chain(it) == "base")
    base_usdc_count = sum(
        1 for it in items if _case_chain(it) == "base" and _case_needs_token(it, "BASE_ETH_USDC")
    )
    if base_eth_count == 0:
        return  # 无 base 主网 case 不做 gate

    required_eth = base_eth_count * _DEFAULT_BASE_ETH_PER_CASE * safety
    required_usdc = base_usdc_count * _DEFAULT_BASE_USDC_PER_CASE * safety

    print(f"=== 余额预检（单机最坏负担：base × {base_eth_count} case；safety={safety}x）===")
    print(
        f"门槛: BASE_ETH ≥ {required_eth:.4f}, "
        f"BASE_ETH_USDC ≥ {required_usdc:.4f} (USDC case = {base_usdc_count})"
    )

    bal_results = await asyncio.gather(*(_query_remote_balance(s) for s in servers))
    failures: list[tuple[dict, float, float]] = []
    for srv, bal in zip(servers, bal_results):
        if bal is None:
            print(f"  {srv['name']}: 查询失败（放行，按 SKIP 处理）")
            continue
        eth = bal.get(("BASE_ETH", "BASE_ETH"), 0.0)
        usdc = bal.get(("BASE_ETH", "BASE_ETH_USDC"), 0.0)
        ok = eth >= required_eth and usdc >= required_usdc
        tag = "OK  " if ok else "WARN"
        print(f"  {srv['name']}: {tag} BASE_ETH={eth:.4f} USDC={usdc:.4f}")
        if not ok:
            failures.append((srv, eth, usdc))

    if failures:
        print(
            f"\n[BALANCE WARN] {len(failures)}/{len(servers)} 台单机最坏预算不足。"
            "动态队列下若某台机分到全部 case，余额可能在中途耗尽，导致 agent 误判 0 分。\n"
            "  建议：充值到所列 wallet 地址；或加 --skip-balance-gate 应急绕过；"
            "或改 --static 静态分配 + 显式让快机/有余额机承担更多。"
        )
        if abort_on_fail:
            sys.exit(2)
    print()


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
    eval_mode: str = "e2e",
    recipe_source: str = "",
    skip_balance_gate: bool = False,
) -> str:
    """动态 worker：从队列持续取 item 执行，直到队列空为止。

    每次只跑 1 个 item，完成后立即从队列取下一个，实现服务器间负载均衡。
    item_results[item_id] = (server_name, rc) 记录每个 item 的执行结果。
    rc=-2 表示 ``balance-skipped``（per-case 余额 gate 不通过，未消耗远端 ssh / agent 资源）。
    """
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        item_id = item["id"]

        # ── per-case 余额 gate：仅 mainnet (base/polygon) case 检查 ─────────────
        # 启动时的 _dispatch_balance_preflight 是"单机最坏负担"全局门槛，但 superfluid
        # upgrade 一次性把 USDC 全 wrap 这种异常消耗会让同机后续 case 在中途看到 0。
        # per-case 实查能捕获到这种"运行中余额耗尽"，标 balance-skipped 而非交给 agent
        # 误判（参见 eval-report 4.1 finding）。SSH 查 ~3s，开销可接受。
        if not skip_balance_gate and _case_chain(item) == "base":
            balances = await _query_remote_balance(server)
            if balances is not None:
                ok, gate_msg = _balance_gate_check_one_case(item, balances)
                if not ok:
                    log_file = log_dir / f"{server['name']}-{item_id}.log"
                    with log_file.open("w", encoding="utf-8") as f:
                        f.write(
                            f"# balance-skipped on {server['name']}: {gate_msg}\n"
                            f"# per-case balance gate; item not dispatched. "
                            f"Top up the agent wallet on this server and re-dispatch with "
                            f"`--item-id {item_id}` to retry.\n"
                        )
                    print(f"[BALANCE-SKIP {server['name']}] item={item_id}: {gate_msg}")
                    item_results[item_id] = (server["name"], -2)
                    queue.task_done()
                    continue

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
            recipe_source=recipe_source,
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
            # SSH 层硬超时：item_timeout + 60s 余量，防止 IAP tunnel / 远端僵尸拖死整个 dispatch。
            # 注：远端命令最外层已套 `timeout (item_timeout+60)s --kill-after=30s`，所以即便
            # 这里 proc.kill() 无法把 SIGHUP 传到远端 process tree，远端进程到点也会被自己 SIGTERM/SIGKILL，
            # 不会形成"孤儿被下一个 SSH 撞上"的同 server 同钱包并发污染（详见 _build_remote_run_cmd）。
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
        # 注：SSH_TIMEOUT (rc=-1) 时也要拉 —— server-side SIGTERM handler 会在
        # timeout 包装的 30s grace 期内跑归档，文件可能已存在；本地无脑 pull 是 best-effort
        # 拿不到也不会出错（_pull_pact_specs 内部 except 兜底）
        await _pull_pact_specs(server, run_name)
        # 拉取服务器端归档的原始 session jsonl 事件（Phase 1: judge 数据源候选）
        # _run_single_oc_task line 608-621 已把 agent jsonl 拷到 ~/.caw-eval/runs/<run>/<item>.jsonl
        # 本拉取写入本地 raw-sessions/<item_id>.jsonl，供 score_traces 直读（Phase 2 接入）
        await _pull_raw_sessions(server, run_name)

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


async def _pull_raw_sessions(server: dict, run_name: str) -> None:
    """从服务器拉回原始 ``<item_id>.jsonl`` session 事件文件到 ``raw-sessions/``。

    服务器端 ``_run_single_oc_task`` 每个 item 跑完后已把 agent 的 jsonl 拷到
    ``~/.caw-eval/runs/<run>/<item_id>.jsonl``（line 608-621）。本函数把这些文件
    tar/scp 回本地 ``~/.caw-eval/runs/<run>/raw-sessions/<item_id>.jsonl``，作为
    judge 评分的"无失真"数据源（Langfuse trace 重建路径会丢失 turn 顺序、截断丢字段，
    见 P0 turn-envelope bug 修复历史）。

    与 ``_pull_pact_specs`` 同样的 ``bash -c 'gcloud ssh ... | tar xzf -'`` 模式：
    asyncio 自身不能转接 ssh.stdout → tar.stdin。
    """
    local_dir = _RUNS_DIR / run_name / "raw-sessions"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = f"~/.caw-eval/runs/{run_name}"
    # 排除 agent_map.jsonl（dispatcher 元数据，不是 session 事件文件）
    remote_cmd = (
        f"sudo su - ubuntu -c 'cd {remote_dir} 2>/dev/null && "
        f'ls *.jsonl 2>/dev/null | grep -v "^agent_map\\.jsonl$" | '
        f"{{ tar czf - -T - 2>/dev/null || true; }}'"
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
            err_tail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
            print(
                f"[WARN] raw-sessions pull failed from {server['name']} rc={proc.returncode} {err_tail}"
            )
    except Exception as exc:
        print(f"[WARN] raw-sessions pull exception from {server['name']}: {exc!r}")


def _upload_partial_sessions(
    run_name: str,
    dataset_name: str,
    skill: str,
    run_description: str,
) -> dict[str, str]:
    """上传 dispatch 完成后留在本地 raw-sessions/ 的 ``<item>.partial.jsonl`` 到 Langfuse。

    背景：远端 SIGTERM 在 `STAGE: session_collected` 之前发生，server-side upload 流程
    跳过；新 SIGTERM handler `_sync_archive_session` 把当前 agent 的 session jsonl 拷到
    ``~/.caw-eval/runs/<run>/<item>.partial.jsonl``，再被 `_pull_raw_sessions` 用
    `ls *.jsonl` 拉回本地。本函数对那些只有 partial 没有完整 session 的 item 补一次上传，
    带 ``incomplete=True`` 标签 + 关联 dataset run，让 judge / score apply 可以检索这些
    case（否则它们在 Langfuse 上完全缺失，下游 `score_traces.py langfuse` iterate
    `dataset_run_items.list` 时一个不漏地跳过）。

    与 ``batch_upload_sessions`` 区别：后者对完整 session（``<item>.jsonl``）按 stem 当 item_id
    用，partial 文件 stem 是 ``<item>.partial`` 会让 dataset_item 查找失败，所以单独写一个
    去掉 ``.partial`` 后缀的版本。

    返回 ``{item_id: trace_id}`` 上传成功的映射；失败/跳过不计入。
    """
    local_dir = _RUNS_DIR / run_name / "raw-sessions"
    if not local_dir.is_dir():
        return {}

    partial_files = sorted(local_dir.glob("*.partial.jsonl"))
    if not partial_files:
        return {}

    # 过滤掉同时存在完整 session 的（不应正常发生，但防御一下）
    eligible: list[tuple[str, Path]] = []
    for pf in partial_files:
        # stem 形如 "<item>.partial"，再去掉 .partial 拿真实 item_id
        item_id = pf.stem
        if item_id.endswith(".partial"):
            item_id = item_id[: -len(".partial")]
        full_path = local_dir / f"{item_id}.jsonl"
        if full_path.exists():
            print(f"  [{item_id}] skip partial upload: 同名完整 session 已存在")
            continue
        eligible.append((item_id, pf))

    if not eligible:
        return {}

    print(f"\n=== 上传 {len(eligible)} 个 partial session (run: {run_name}) ===")

    lf = get_langfuse_client()
    ds_items = get_dataset_items(dataset_name)
    meta_to_langfuse = {item["id"]: item["langfuse_id"] for item in ds_items}
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
    for item_id, partial_file in eligible:
        trace_id = str(uuid.uuid4())
        size_kb = partial_file.stat().st_size / 1024
        print(f"  [{item_id}] uploading partial ({size_kb:.0f}KB, trace={trace_id[:8]}...)")

        ctx = dict(item_context.get(item_id, {"item_id": item_id}))
        ctx["incomplete"] = True
        ctx["partial_reason"] = "sigterm_timeout"

        result_trace_id = upload_session(
            str(partial_file),
            skill,
            trace_id=trace_id,
            extra_metadata=ctx,
        )
        if not result_trace_id:
            print(f"    [ERROR] partial upload failed for {item_id}")
            continue

        trace_map[item_id] = result_trace_id
        langfuse_item_id = meta_to_langfuse.get(item_id)
        if langfuse_item_id:
            link_to_dataset_run(lf, langfuse_item_id, run_name, result_trace_id, run_description)
        else:
            print(f"    [WARN] dataset item not found for {item_id}, trace 已上传但未关联 run")

    lf.flush()

    # 写 trace_map.partial.json，供事后审计或 score_traces 直接使用（不动主 trace_map.json，
    # 避免与 server-side session_collected 上传时写的同名文件竞争 fcntl 锁）。
    if trace_map:
        partial_map_path = _RUNS_DIR / run_name / "trace_map.partial.json"
        try:
            partial_map_path.write_text(json.dumps(trace_map, indent=2, ensure_ascii=False))
            print(f"  [SAVED] {len(trace_map)} partial trace mapping(s) → {partial_map_path.name}")
        except Exception as exc:
            print(f"  [WARN] write trace_map.partial.json failed: {exc!r}")

    return trace_map


async def _caw_health_check(srv: dict, min_tss_version: str) -> tuple[dict, bool, str]:
    """SSH 单台服务器，对 caw CLI 跑四项健康预检：

    1. ``caw status`` JSON 中 ``healthy`` 字段（后端 HealthAPI 可达）
    2. ``caw node health`` exit code 0（本地 TSS binary / db / config / keyfile / 进程完整性 +
       keyfile mode=600）
    3. ``caw node status`` 中 ``remote.online == true``（TSS websocket 与 backend 连通；进程活着
       不代表 backend 视角能签 — 网络抖动 / push offline 都会让 online=false）
    4. ``<tss_binary> version`` ≥ ``min_tss_version``（避免 caw 在 sign/tx 阶段触发
       ``ensureRuntimeTSSNodeMinVersion`` 二进制热升级 → ETXTBSY）

    返回 ``(srv, healthy, info)``：``healthy`` 为四项全过；``info`` 是用于打印的诊断短串
    （成功时是 ``"tss=v0.12.20 online=true"`` 等信息，失败时是失败原因）。
    """
    inner = (
        "set -o pipefail; "
        "export PATH=/home/ubuntu/.cobo-agentic-wallet/bin:/home/ubuntu/.npm-global/bin:$PATH; "
        # 1. caw status backend healthy
        'S=$(caw status 2>&1) || { echo "FAIL caw_status: cmd_error: ${S:0:200}"; exit 1; }; '
        'echo "$S" | python3 -c \'import sys,json;'
        ' sys.exit(0 if json.load(sys.stdin).get("healthy") else 1)\' '
        '  || { echo "FAIL caw_status: healthy=false"; exit 1; }; '
        # 2. caw node health (binary/db/config/keyfile/process integrity + keyfile mode=600)
        "caw node health >/dev/null 2>&1 "
        "  || { echo 'FAIL caw_node_health: missing files / process / wrong keyfile mode'; exit 1; }; "
        # 3. caw node status — backend 视角 TSS 是否 online（websocket 连接活着）
        "NS=$(caw node status 2>&1) "
        '  || { echo "FAIL caw_node_status: cmd_error: ${NS:0:200}"; exit 1; }; '
        'ONLINE=$(echo "$NS" | python3 -c \'import sys,json;'
        ' d=json.load(sys.stdin); r=d.get("remote",{});'
        ' print("true" if r.get("online") else "false")\' 2>/dev/null); '
        '[ "$ONLINE" = "true" ] '
        '  || { echo "FAIL caw_node_status: backend reports remote.online=false (TSS 进程活着但 websocket 未连 backend)"; exit 1; }; '
        # 4. TSS binary version >= min
        "INFO=$(caw node info 2>/dev/null) "
        "  || { echo 'FAIL caw_node_info: cmd_error'; exit 1; }; "
        'BIN=$(echo "$INFO" | python3 -c \'import sys,json;'
        ' print(json.load(sys.stdin).get("binary_path",""))\' 2>/dev/null); '
        '[ -x "$BIN" ] '
        '  || { echo "FAIL: TSS binary missing or not exec at $BIN"; exit 1; }; '
        'RAW=$("$BIN" version 2>&1 || "$BIN" --version 2>&1) '
        "  || { echo 'FAIL: tss version cmd error'; exit 1; }; "
        "V=$(echo \"$RAW\" | grep -oE 'v?[0-9]+\\.[0-9]+\\.[0-9]+' | head -1); "
        '[ -n "$V" ] '
        '  || { echo "FAIL: cannot parse TSS version (raw=${RAW:0:120})"; exit 1; }; '
        f"MIN={shlex.quote(min_tss_version)}; "
        'case "$V" in v*) ;; *) V="v$V";; esac; '
        'LO=$(printf \'%s\\n%s\\n\' "$V" "$MIN" | sort -V | head -1); '
        '[ "$LO" = "$MIN" ] '
        '  || { echo "FAIL: TSS version $V < required $MIN (caw sign 阶段会尝试热升级二进制 → ETXTBSY)"; exit 1; }; '
        'echo "OK tss=$V online=true"'
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return srv, False, "SSH timeout >45s"
    out = stdout.decode("utf-8", "replace").strip()
    last_line = out.splitlines()[-1] if out else ""
    if last_line.startswith("OK "):
        return srv, True, last_line[3:]
    # 去掉 shell 错误消息里的冗余 "FAIL " / "FAIL: " 前缀（外层打印 marker 已表达失败状态）
    info = last_line
    for prefix in ("FAIL: ", "FAIL "):
        if info.startswith(prefix):
            info = info[len(prefix) :]
            break
    if not info:
        err_tail = stderr.decode("utf-8", "replace").strip()[-200:]
        info = f"empty output (stderr={err_tail!r})"
    return srv, False, info


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
    eval_mode: str = "e2e",
    recipe_source: str = "",
    skip_caw_preflight: bool = False,
    skip_balance_gate: bool = False,
    abort_on_balance: bool = False,
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

    # ── caw 健康预检（status / node health / TSS version）──────────────────────────
    # 背景（2026-04-27 三场 eval 同根因）：caw v0.2.79 在每次 sign/tx 前会调
    # ``ensureRuntimeTSSNodeMinVersion``，如果运行中 cobo-tss-node 版本低于服务端要求，
    # caw 会 ``os.WriteFile`` 直接覆盖正在执行的 binary → ETXTBSY (text file busy) →
    # 后续所有 sign 操作失败。dispatch 入口提前对每台服务器跑：
    #   1. ``caw status`` healthy=true（后端可达）
    #   2. ``caw node health`` 退出码 0（本地 TSS 文件 + 进程完整性）
    #   3. ``<tss_bin> version`` ≥ ``CAW_EVAL_PREFLIGHT_MIN_TSS_VERSION``（默认 SDK baseline，
    #      可通过环境变量提到当前服务端推送的更高 min，如 v0.12.20）
    # 任一服务器任一项失败 → 直接 abort，避免在 Base 主网真金跑了 ~30 分钟才发现。
    # ``--skip-caw-preflight`` 是应急开关（不推荐，仅用于诊断）。
    if not skip_caw_preflight:
        min_tss = os.environ.get(
            "CAW_EVAL_PREFLIGHT_MIN_TSS_VERSION", _DEFAULT_PREFLIGHT_MIN_TSS_VERSION
        )
        print(f"=== caw 健康预检（status / node health / TSS ≥ {min_tss}）===")
        caw_results = await asyncio.gather(*(_caw_health_check(s, min_tss) for s in servers))
        caw_failures: list[tuple[dict, str]] = []
        for srv, ok, info in caw_results:
            print(f"  {srv['name']}: {'OK ' if ok else 'FAIL '}{info}")
            if not ok:
                caw_failures.append((srv, info))
        if caw_failures:
            print(
                f"\n[ABORT] {len(caw_failures)}/{len(servers)} 台 caw 健康预检失败。"
                "Base 主网真金评测前要求所有服务器健康，避免运行中触发 TSS 二进制热升级 ETXTBSY。\n"
                "  常见修复：\n"
                "    - healthy=false: 检查 backend 可达性 / API 凭据\n"
                "    - caw_node_health: 重新跑 caw onboard 或检查 keyfile 权限 (chmod 600)\n"
                "    - TSS version <: SSH 上去 `pkill -f cobo-tss-node` 后再跑任意 caw tx 命令"
                " 触发安全升级，或手动替换 TSS binary 到达 min 版本\n"
                "  应急绕过（不推荐）：dispatch 加 --skip-caw-preflight"
            )
            sys.exit(2)
        print()

    # ── 余额预检（mainnet 评测专用）─────────────────────────────────────────────
    # 算"单机最坏负担"门槛：动态队列下若某台机分到全部 mainnet case，需要至少多少 ETH/USDC。
    # 默认 warn 不阻塞；--abort-on-balance 时门槛不达 → 直接退出。
    # --skip-balance-gate 跳过整个余额检查（含 worker 内 per-case gate）。
    if not skip_balance_gate:
        try:
            await _dispatch_balance_preflight(items, servers, abort_on_fail=abort_on_balance)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"[WARN] 余额预检异常（跳过门槛，继续）: {exc!r}")

    # ── CLI 健康预检：确保每台 openclaw agents add/delete 在 30s 内响应 ──────────
    # 背景：openclaw 没有 session GC，sessions.json 累积后 agents add 可能超 30s 静默挂
    #       （remote cmd_run 依旧 exit 0 → dispatch 误判为 OK）。启动前 smoke test，
    #       慢于 threshold 的服务器直接剔除，避免 item 分下去后才失败。
    #       修复方式：登录服务器跑 ~/.agents/skills/caw-eval/scripts/prune_openclaw_sessions.sh
    # 阈值 15s → 30s → 60s（2026-04-27）：旧 GPT 服务器 070641 实测 add 23s + delete 22s
    # （sessions=2174 行，该机器已下线、由 test0 替换）。接近 30s 阈值导致 dispatch 启动时
    # 偶发 timeout；同期 test8 实测仅 4s/3s 但也被剔除，怀疑 IAP tunnel 建连抖动叠加 30s
    # 偏紧。60s 给 add/delete 各 2.5x 余量。根治需升级机型到 e2-medium 或重建到 AMD EPYC zone。
    SMOKE_TIMEOUT_SEC = 60
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
    recipe_gateway_active = eval_mode == "pact" and recipe_source == "seed"
    if recipe_gateway_active:
        await _setup_gateway_recipe_env(servers)
    else:
        # 标准模式 / 其他模式：幂等清理可能残留的 systemd drop-in 与 /tmp/caw-eval-recipe.json，
        # 防止上一轮 recipe 评测残留继续把 caw recipe search 短路到文件分支，污染真实后端基线。
        # _teardown_gateway_recipe_env 的命令链用 `rm -f` + systemctl restart，drop-in 不存在也无副作用。
        await _teardown_gateway_recipe_env(servers)

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
                    recipe_source=recipe_source,
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
            await queue.put(item)  # 整个 dict 入队，worker 内可读 metadata 做 per-case gate

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
                recipe_source=recipe_source,
                skip_balance_gate=skip_balance_gate,
            )
            for srv in servers
        ]
        await asyncio.gather(*workers)

        # 上传 SIGTERM 抢救出来的 partial session：worker 已通过 _pull_raw_sessions
        # 把 *.partial.jsonl 拉到本地 raw-sessions/，但 server-side upload 流程没跑
        # （SIGTERM 在 session_collected 之前），所以 partial trace 不在 Langfuse。
        # 这里补一遍上传，带 incomplete=True 标签，让下游 score_traces 能检索到。
        run_description = (
            f"openclaw eval | model={model_full or model} | "
            f"dataset={dataset_name} | eval_mode={eval_mode} | recipe_source={recipe_source}"
        )
        try:
            _upload_partial_sessions(run_name, dataset_name, skill, run_description)
        except Exception as exc:
            print(f"[WARN] partial session upload exception: {exc!r}")

        print("\n=== 完成 ===")
        failed_items: list[str] = []
        balance_skipped: list[str] = []
        for item_id, (srv_name, rc) in sorted(item_results.items()):
            if rc == 0:
                status = "OK"
            elif rc == -2:
                status = "BALANCE-SKIP"
                balance_skipped.append(item_id)
            else:
                status = f"FAIL rc={rc}"
                failed_items.append(item_id)
            print(f"  [{srv_name}] {item_id}: {status}")

        if balance_skipped:
            print(
                f"\n余额不足跳过 items ({len(balance_skipped)}): {balance_skipped}\n"
                f"  这些 case 未被 dispatch 到 agent（避免 USDC=0 误判 0 分）。\n"
                f"  充值目标 wallet 后，重跑：--item-id {' '.join(balance_skipped)}"
            )
        if failed_items:
            print(f"\n失败 items: {failed_items}")
            print(f"查看日志: {log_dir}/<server>-<item_id>.log")
            print(f"重跑命令示例: --item-id {' '.join(failed_items)}")
            sys.exit(1)
        elif not balance_skipped:
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
    p_run.add_argument("--dataset-name", default="standard-test-v3")
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
        choices=["e2e", "pact", "onboard", "standard", "recipe"],
        default="e2e",
        help="评测模式: e2e (默认，全流程含 task_completion) / pact (仅评 pact 构造) / onboard"
        "；老值 standard→e2e、recipe→pact 仍接受",
    )
    p_run.add_argument(
        "--recipe-source",
        choices=["real", "seed", "empty"],
        default="",
        help="Recipe 来源: real (调真实 backend) / seed (注入 dataset 的 recipe)。"
        "openclaw 不支持 empty (仅 cc 对照组)",
    )
    p_run.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "cc_real_recipe", "openclaw", "oc_real_recipe"],
        default="",
        help="[已弃用] 用 --recipe-source 替代",
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
    p_import.add_argument("--dataset-name", default="standard-test-v3")
    p_import.add_argument("--run-name", required=True)
    p_import.add_argument("--item-id", nargs="*", help="只导入指定 item")
    p_import.add_argument("--export-dir", help="session 导出目录（默认 /tmp/eval-sessions）")

    # ── upload
    p_upload = sub.add_parser("upload", help="上传 session 到 Langfuse")
    p_upload.add_argument("--run-name", required=True)
    p_upload.add_argument("--dataset-name", default="standard-test-v3")
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
    p_dispatch.add_argument("--dataset-name", default="standard-test-v3")
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
        choices=["e2e", "pact", "onboard", "standard", "recipe"],
        default="e2e",
        help="评测模式: e2e (默认，全流程含 task_completion) / pact (仅评 pact 构造) / onboard"
        "；老值 standard→e2e、recipe→pact 仍接受",
    )
    p_dispatch.add_argument(
        "--recipe-source",
        choices=["real", "seed", "empty"],
        default="",
        help="Recipe 来源: real (调真实 backend) / seed (注入 dataset 的 recipe)。"
        "openclaw 不支持 empty (仅 cc 对照组)",
    )
    p_dispatch.add_argument(
        "--recipe-mode",
        choices=["cc_with_recipe", "cc_no_recipe", "cc_real_recipe", "openclaw", "oc_real_recipe"],
        default="",
        help="[已弃用] 用 --recipe-source 替代",
    )
    p_dispatch.add_argument(
        "--skip-caw-preflight",
        action="store_true",
        help=(
            "跳过 caw 健康预检 (status / node health / TSS version)。"
            "应急开关，常规 Base 主网评测请勿使用 — caw v0.2.79 在 sign/tx 阶段触发的"
            "TSS 二进制热升级 ETXTBSY 故障在历史评测中曾命中 13/17 case。"
        ),
    )
    p_dispatch.add_argument(
        "--skip-balance-gate",
        action="store_true",
        help=(
            "跳过余额 gate（含启动预检和 worker 内 per-case 实查）。"
            "默认开启 mainnet (chain∈{base, polygon}) case 的余额检查；"
            "启动预检 warn 不阻塞，per-case 实查会标 balance-skipped 不消耗资源。"
            "当余额来源不依赖 caw wallet（如 sign-only）或调试时使用。"
        ),
    )
    p_dispatch.add_argument(
        "--abort-on-balance",
        action="store_true",
        help=(
            "余额预检不达单机最坏负担门槛时直接 abort（默认仅 warn）。"
            "正式 Base 主网评测推荐打开；演练 / 调试时可省。"
        ),
    )

    args = parser.parse_args()

    # 数据集 name / id / URL 三种形式统一规范化为 name。
    if getattr(args, "dataset_name", None):
        try:
            args.dataset_name = resolve_dataset(args.dataset_name)
        except ValueError as e:
            print(f"[ERROR] {e}", flush=True)
            sys.exit(2)
        if args.cmd == "dispatch":
            print_dataset_summary(args.dataset_name)

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
                eval_mode=_normalize_eval_mode(args.eval_mode),
                recipe_source=_normalize_recipe_source(args.recipe_source, args.recipe_mode),
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
                eval_mode=_normalize_eval_mode(args.eval_mode),
                recipe_source=_normalize_recipe_source(args.recipe_source, args.recipe_mode),
                skip_caw_preflight=args.skip_caw_preflight,
                skip_balance_gate=args.skip_balance_gate,
                abort_on_balance=args.abort_on_balance,
            )
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
