#!/usr/bin/env python3
"""
Script 3: 从 Langfuse 拉取 trace 数据，按 S1-S3 各阶段评分，结果直接写回 Langfuse

用法:
    # 阶段一：生成 LLM judge prompt 文件（由 Copilot 通过 task subagent 执行评分）
    python score_traces.py --run-name eval-run-20250101 --dump-judge-requests /tmp/judge_req.json
    python score_traces.py session --session /path/to/sessions_dir/ --dump-judge-requests /tmp/judge_req.json

    # 阶段二：Copilot 通过 openclaw task subagent 执行评分，将结果写入 /tmp/judge_results.json

    # 阶段三：读取 judge 结果并上传到 Langfuse
    python score_traces.py --run-name eval-run-20250101 --judge-results /tmp/judge_results.json
    python score_traces.py session --session /path/to/sessions_dir/ --judge-results /tmp/judge_results.json

    # 单独对单个 trace 评分（使用启发式回退）
    python score_traces.py --trace-id <trace_id>

    # 生成评分报告（不上传到 Langfuse）
    python score_traces.py --run-name X --judge-results /tmp/r.json --report --dry-run

    # 直接从本地 session .jsonl 文件评分（带 item 上下文）
    python score_traces.py session --session /path/to/session.jsonl \
        --item-id E2E-01L1 --dataset-name caw-agent-eval-v1 \
        --judge-results /tmp/judge_results.json

评分架构:
    每个 trace 的评分结果会创建一个新的 Langfuse Trace（evaluator 类型），
    包含 S1-S3 各阶段的详细评分（输入到该阶段 trace 的 output 字段），
    同时将各维度分数作为 Langfuse Score 上传到原始 trace。
    评分和 trace 创建均通过 Langfuse SDK 直接写入，无需 CAW 后端。

阶段权重:
    S1 意图解析   20%  | 评估 operation_type/实体提取/隐含约束
    S2 Pact 协商  40%  | 评估 4-item preview/权限范围/pact submit/用户确认
    S3 交易执行   40%  | 评估命令选择/参数准确性/错误处理/结果验证

环境变量:
    LANGFUSE_HOST          - Langfuse 服务地址（默认 sandbox）
    LANGFUSE_PUBLIC_KEY    - Langfuse 公钥
    LANGFUSE_SECRET_KEY    - Langfuse 私钥

评分机制:
    主评分: 由 Copilot 通过 openclaw task subagent（task 工具）执行 LLM-as-a-Judge 评分。
            脚本生成 judge prompt 文件（--dump-judge-requests），Copilot 读取后调度
            subagent 执行，将结果写入 judge results 文件（--judge-results）。
    备用评分: 未提供 --judge-results 时自动退回启发式规则（关键词匹配），确保流程不中断。
    评分来源标记: caw.scoring_source = 1.0（subagent）/ 0.0（启发式）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 自动加载同目录下的 .env（不覆盖已设置的环境变量）
load_dotenv(Path(__file__).parent / ".env", override=False)

# ── Langfuse 凭证常量 ──────────────────────────────────────────────────────────
# score_traces.py 操作 *results* project（写入评分和 scoring trace）。
# 与 dataset project（generate_dataset.py / run_eval.py list）使用不同凭证。

_DEFAULT_LF_HOST = "https://langfuse.1cobo.com"


# ── Langfuse client helper ────────────────────────────────────────────────────

def _make_langfuse() -> Any:
    """Create a Langfuse client (single unified project for both dataset and results).

    Priority: LANGFUSE_DATASET_* → LANGFUSE_* → default host.
    """
    from langfuse import Langfuse

    def _pick(specific: str, generic: str, default: str = "") -> str:
        return os.environ.get(specific) or os.environ.get(generic) or default

    host = _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST", _DEFAULT_LF_HOST)
    public_key = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    secret_key = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        print("[WARN] Langfuse credentials not set. "
              "Set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY "
              "(or LANGFUSE_DATASET_PUBLIC_KEY + LANGFUSE_DATASET_SECRET_KEY).")

    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


# Alias for dataset reads — same project
_make_dataset_langfuse = _make_langfuse


# ── Stage weights ─────────────────────────────────────────────────────────────
#
# CAW workflow has 3 natural phases that map directly to what the skill guides:
#   S1 意图解析   — first agent response: understand what and where
#   S2 Pact 协商  — propose minimum-scope pact, get user confirmation, submit
#   S3 交易执行   — run caw tx transfer / caw tx call under the pact,
#                  verify result and surface status
#
# S2+S3 carry equal high weight because pact scope correctness and execution
# correctness (including result verification) are the two primary failure modes.

STAGE_WEIGHTS = {
    "s1": 0.20,
    "s2": 0.40,
    "s3": 0.40,
}


# ── Stage content extractor ───────────────────────────────────────────────────

def _obs_text(obs: Any) -> str:
    """Extract combined input+output text from an observation."""
    parts: list[str] = []
    if hasattr(obs, "input") and obs.input:
        parts.append(str(obs.input))
    if hasattr(obs, "output") and obs.output:
        parts.append(str(obs.output))
    return "\n".join(parts)


def extract_stage_content(trace: Any) -> dict[str, str]:
    """
    从 Langfuse trace 的 observations 中提取 S1-S3 各阶段相关文本。

    span 命名规范（由 otel_report.py 生成）:
      turn:N    → 对话轮次（含 LLM 输入输出）
      exec:caw  → CAW CLI 工具调用结果
      session:X → 根 span

    S1 (意图解析):    第一个 turn span（或 trace 开头）
    S2 (Pact 协商):   所有 exec:caw pact 调用 + 包含 pact 关键词的 turn
    S3 (交易执行):    所有 exec:caw tx / exec:caw transfer 调用 + 最后一个 turn
    """
    obs_list = getattr(trace, "observations", None) or []
    try:
        obs_list = sorted(obs_list, key=lambda o: getattr(o, "start_time", None) or "")
    except TypeError:
        pass

    turn_texts: list[str] = []
    pact_texts: list[str] = []
    tx_texts: list[str] = []
    full_parts: list[str] = []

    for obs in obs_list:
        name = (getattr(obs, "name", "") or "").lower()
        text = _obs_text(obs)
        if not text.strip():
            continue
        full_parts.append(text)

        if name.startswith("turn:"):
            turn_texts.append(text)

        if "exec:caw pact" in name or ("caw pact" in text.lower() and "exec" in name):
            pact_texts.append(text)
        elif any(s in text.lower() for s in ("caw pact submit", "caw pact create")):
            pact_texts.append(text)

        if any(s in name for s in ("exec:caw tx", "exec:caw transfer", "exec:caw swap",
                                    "exec:caw bridge", "exec:caw deposit", "exec:caw call")):
            tx_texts.append(text)
        elif any(s in text.lower() for s in ("caw tx transfer", "caw tx call", "caw transfer --to",
                                              "exactinputsingle", "--pact-id")):
            tx_texts.append(text)

    if not full_parts:
        for attr in ("output", "input"):
            val = getattr(trace, attr, None)
            if val:
                full_parts.append(str(val))

    full_text = "\n\n".join(full_parts)

    # S1: first turn (intent parsing)
    s1 = turn_texts[0] if turn_texts else full_text[:2000]
    # S2: pact-related turns + pact exec spans
    pact_turn_texts = [t for t in turn_texts if any(
        kw in t.lower() for kw in ("pact", "pact_id", "caw pact", "执行计划", "完成条件",
                                    "policies", "permission", "确认", "confirm", "shall i")
    )]
    s2 = "\n\n".join(pact_texts + pact_turn_texts) or _grep_block(full_text, [
        "caw pact", "pact submit", "execution plan", "policies", "completion conditions"
    ])
    # S3: tx execution spans + last turn (result verification)
    last_turn = turn_texts[-1] if len(turn_texts) > 1 else ""
    s3 = "\n\n".join(tx_texts + ([last_turn] if last_turn else [])) or full_text

    return {
        "s1": s1 or full_text[:2000],
        "s2": s2 or full_text[:3000],
        "s3": s3 or full_text,
        "full": full_text,
    }


def _grep_block(text: str, signals: list[str], window: int = 500) -> str:
    """Return the ±window chars around the first matching signal in text."""
    lower_text = text.lower()
    for sig in signals:
        idx = lower_text.find(sig.lower())
        if idx >= 0:
            start = max(0, idx - 200)
            end = min(len(text), idx + window)
            return text[start:end]
    return ""


# ── Session-based stage extraction (no Langfuse read required) ───────────────

def _parse_session_file(path: str) -> dict:
    """
    Parse a session .jsonl file into a structured dict.

    Supports two formats:
      - OpenClaw otel format: type=session + type=message events, id/toolCallId keys
      - Claude Code native format: type=user/assistant events, uuid/sessionId keys,
        tool_use/tool_result content blocks

    Returns {session_id, started_at, cwd, model, provider, messages, order}.
    """
    import pathlib
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    session_id = ""
    started_at = ""
    cwd = ""
    model = ""
    provider = ""
    messages: dict[str, dict] = {}
    order: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev_type = ev.get("type", "")

        if ev_type == "session":
            # OpenClaw otel format: dedicated session event
            session_id = ev.get("id", "")
            started_at = ev.get("timestamp", "")
            cwd = ev.get("cwd", "")

        elif ev_type == "message":
            # OpenClaw otel format: dedicated message events
            ev_id = ev.get("id", "")
            msg = ev.get("message", {})
            if not model and msg.get("model"):
                model = msg.get("model", "")
            if not provider and msg.get("provider"):
                provider = msg.get("provider", "")
            if ev_id:
                messages[ev_id] = ev
                order.append(ev_id)

        elif ev_type in ("user", "assistant"):
            # Claude Code native format: user/assistant events with uuid + sessionId
            if not session_id and ev.get("sessionId"):
                session_id = ev["sessionId"]
            if not cwd and ev.get("cwd"):
                cwd = ev["cwd"]
            if not started_at and ev.get("timestamp"):
                started_at = ev["timestamp"]
            ev_id = ev.get("uuid") or ev.get("id", "")
            if ev_id and ev_id not in messages:
                # Normalize tool_use blocks → toolCall; tool_result → toolResult role
                msg = ev.get("message", {})
                role = msg.get("role", ev_type)
                content = msg.get("content", [])
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                normalized: list[dict] = []
                for block in content:
                    if block.get("type") == "tool_use":
                        normalized.append({
                            "type": "toolCall",
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": block.get("input", {}),
                        })
                    else:
                        normalized.append(block)
                normalized_ev = {**ev, "message": {**msg, "role": role, "content": normalized}}
                messages[ev_id] = normalized_ev
                order.append(ev_id)

    if not session_id:
        session_id = pathlib.Path(path).stem

    return {
        "session_id": session_id,
        "started_at": started_at,
        "cwd": cwd,
        "model": model,
        "provider": provider,
        "messages": messages,
        "order": order,
    }


def _session_message_events(session: dict) -> list[dict]:
    """Return message events in chronological order."""
    order: list[str] = session.get("order", [])
    messages: dict[str, dict] = session.get("messages", {})
    return [messages[eid] for eid in order if eid in messages]


def _session_tool_result_index(events: list[dict]) -> dict[str, dict]:
    """Build {toolCallId: synthetic_event} from toolResult events (both formats)."""
    idx: dict[str, dict] = {}
    for ev in events:
        msg = ev.get("message", {})
        # OpenClaw otel format: dedicated toolResult event
        if msg.get("role") == "toolResult" and msg.get("toolCallId"):
            idx[msg["toolCallId"]] = ev
        # Claude Code native format: tool_result blocks inside user events
        elif msg.get("role") == "user":
            for block in msg.get("content", []):
                if block.get("type") == "tool_result" and block.get("tool_use_id"):
                    raw = block.get("content", [])
                    if isinstance(raw, str):
                        raw = [{"type": "text", "text": raw}]
                    idx[block["tool_use_id"]] = {
                        "message": {
                            "role": "toolResult",
                            "toolCallId": block["tool_use_id"],
                            "content": raw,
                        }
                    }
    return idx


def extract_stage_content_from_session(session: dict) -> dict[str, str]:
    """
    从本地 session dict（由 _parse_session_file() 返回）提取 S1-S3 各阶段内容。

    S1 (意图解析):   第一条 user 消息 + 第一条 assistant 回复（第一个工具调用前）
    S2 (Pact 协商):  所有 caw pact submit/create 工具调用 + 含 pact 提案文本的 assistant 消息
    S3 (交易执行):   所有 caw tx transfer/call 工具调用及其结果 + 最后一条 assistant 文本（结果验证）
    """
    evts = _session_message_events(session)
    tr_idx = _session_tool_result_index(evts)

    assistant_msgs = [e for e in evts if e.get("message", {}).get("role") == "assistant"]
    user_msgs = [e for e in evts if e.get("message", {}).get("role") == "user"]

    def get_text_blocks(ev: dict) -> list[str]:
        content = ev.get("message", {}).get("content", [])
        return [b.get("text", "") for b in content if b.get("type") == "text" and b.get("text")]

    def get_tool_calls(ev: dict) -> list[dict]:
        content = ev.get("message", {}).get("content", [])
        return [b for b in content if b.get("type") == "toolCall"]

    def get_tool_result_text(call_id: str) -> str:
        result_ev = tr_idx.get(call_id)
        if not result_ev:
            return ""
        for b in result_ev.get("message", {}).get("content", []):
            if b.get("type") == "text":
                return b.get("text", "")[:600]
        return ""

    def is_pact_call(tc: dict) -> bool:
        name = tc.get("name", "").lower()
        cmd = tc.get("arguments", {}).get("command", "").lower()
        return "pact" in name or (bool(cmd) and "caw pact" in cmd)

    def is_tx_call(tc: dict) -> bool:
        cmd = tc.get("arguments", {}).get("command", "").lower()
        tx_cmds = ("caw tx transfer", "caw tx call", "caw transfer --to", "caw tx sign")
        return any(kw in cmd for kw in tx_cmds)

    # S1: first user message + first assistant response (before any tool calls)
    s1_parts: list[str] = []
    if user_msgs:
        texts = get_text_blocks(user_msgs[0])
        s1_parts.append(f"User: {' '.join(texts)[:800]}")
    for ev in assistant_msgs:
        texts = get_text_blocks(ev)
        tools = get_tool_calls(ev)
        if texts:
            s1_parts.append(f"Assistant: {' '.join(texts)[:1200]}")
        if tools:
            break
    s1 = "\n".join(s1_parts)

    # S2: pact tool calls + assistant messages containing pact proposals
    pact_keywords = ("pact", "执行计划", "execution plan", "policies", "completion conditions",
                     "完成条件", "确认", "confirm", "shall i", "以下操作", "is this correct")
    s2_items: list[dict] = []
    s2_texts: list[str] = []
    for ev in assistant_msgs:
        texts = get_text_blocks(ev)
        tools = get_tool_calls(ev)
        # Include assistant text that looks like a pact proposal
        if texts:
            combined = " ".join(texts)
            if any(kw in combined.lower() for kw in pact_keywords):
                s2_texts.append(combined[:2000])
        # Collect pact tool calls
        for tc in tools:
            if is_pact_call(tc):
                cmd = tc.get("arguments", {}).get("command", "") if tc.get("name") == "exec" else ""
                s2_items.append({
                    "command": cmd or tc.get("name", ""),
                    "arguments": tc.get("arguments", {}),
                    "result": get_tool_result_text(tc.get("id", "")),
                })
    s2_parts = s2_texts + (
        [json.dumps(s2_items, ensure_ascii=False, indent=2)] if s2_items else []
    )
    s2 = "\n---\n".join(s2_parts) or "No pact operations found"

    # S3: transaction execution tool calls (non-pact) + last assistant message
    s3_items: list[dict] = []
    for ev in assistant_msgs:
        for tc in get_tool_calls(ev):
            if is_tx_call(tc):
                cmd = tc.get("arguments", {}).get("command", "")
                s3_items.append({
                    "command": cmd[:400],
                    "result": get_tool_result_text(tc.get("id", "")),
                })
    # Append last assistant text (result verification)
    last_assistant_text = ""
    for ev in reversed(assistant_msgs):
        texts = get_text_blocks(ev)
        if texts:
            last_assistant_text = " ".join(texts)[:3000]
            break
    s3_exec = json.dumps(s3_items[:20], ensure_ascii=False, indent=2) if s3_items else "No tx execution calls found"
    s3 = (s3_exec + "\n---\n" + last_assistant_text) if last_assistant_text else s3_exec

    # Full conversation summary
    full_parts: list[str] = []
    for ev in evts:
        role = ev.get("message", {}).get("role", "")
        if role in ("user", "assistant"):
            texts = get_text_blocks(ev)
            tools = get_tool_calls(ev)
            if texts:
                full_parts.append(f"[{role.upper()}] {' '.join(texts)[:300]}")
            for tc in tools:
                cmd = (tc.get("arguments", {}).get("command", "")
                       if tc.get("name") == "exec" else "")
                full_parts.append(
                    f"[TOOL:{tc.get('name','')}] {(cmd or json.dumps(tc.get('arguments', {})))[:200]}"
                )
    full = "\n".join(full_parts)

    return {
        "s1": s1[:3000] or full[:2000],
        "s2": s2[:4000] or full[:3000],
        "s3": s3[:4000] or full,
        "full": full[:8000],
    }


# ── Stage evaluators ──────────────────────────────────────────────────────────

def score_s1_intent_parsing(content: str, expected: dict, metadata: dict) -> dict[str, Any]:
    """
    S1 意图解析 (权重 20%)

    评估 agent 从 user_message 中正确提取意图和关键实体的能力。
    依据 SKILL.md 要求: Recipient, amount, and chain are explicit; ask if anything is ambiguous.

    维度:
      - operation_type_accuracy (40%): 识别正确的操作类型 (transfer/swap/lend/bridge/dca/query)
      - entity_extraction       (40%): 提取 token/amount/chain/address 实体
      - constraint_recognition  (20%): 识别隐含约束 (滑点上限/定投周期/gas 预留)
    """
    hints = expected.get("pact_hints", {})
    operation_type = hints.get("operation_type", "")
    tags = metadata.get("tags", [])
    text_lower = content.lower()

    # operation_type_accuracy
    op_signals = {
        "transfer": ["transfer", "转账", "发送", "转", "send"],
        "swap": ["swap", "兑换", "换", "exchange"],
        "lend": ["lend", "borrow", "deposit", "aave", "存", "借"],
        "bridge": ["bridge", "跨链", "桥接"],
        "dca": ["dca", "定投", "weekly", "daily", "每天", "每周", "recurring"],
        "query": ["查询", "query", "帮我看", "利率", "balance", "余额"],
        "multi_step": ["步骤", "然后", "先", "再", "step"],
    }
    op_found = any(
        any(sig in text_lower for sig in sigs)
        for op, sigs in op_signals.items()
        if op == operation_type
    )
    op_score = 9.0 if op_found else 4.0

    # entity_extraction
    entity_checks: list[bool] = []
    for key in ("token", "token_in", "token_out", "amount", "amount_in", "chain"):
        val = hints.get(key, "")
        if val and val.lower() not in ("multi", ""):
            entity_checks.append(val.lower() in text_lower)
    entity_score = (sum(entity_checks) / max(len(entity_checks), 1)) * 9 + 1 if entity_checks else 6.0

    # constraint_recognition
    constraint_tags = {"slippage_constraint", "amount_cap", "duration_limited", "gas_reservation"}
    has_constraints = bool(constraint_tags.intersection(set(tags)))
    constraint_signals = ["滑点", "slippage", "最多", "不超过", "限制", "每次", "gas", "persist", "周期"]
    if has_constraints:
        found = sum(1 for s in constraint_signals if s in text_lower)
        constraint_score = min(10.0, 5.0 + found * 1.5)
    else:
        constraint_score = 9.0

    weights = {"op": 0.40, "entity": 0.40, "constraint": 0.20}
    weighted = (
        op_score * weights["op"]
        + entity_score * weights["entity"]
        + constraint_score * weights["constraint"]
    )
    return {
        "stage": "S1",
        "operation_type_accuracy": round(op_score, 1),
        "entity_extraction": round(entity_score, 1),
        "constraint_recognition": round(constraint_score, 1),
        "stage_score": round(weighted, 2),
    }


def score_s2_pact_negotiation(content: str, expected: dict, metadata: dict) -> dict[str, Any]:
    """
    S2 Pact 协商 (权重 40%)

    评估 agent 是否按 SKILL.md Pact 协商规范操作：
    - "Negotiate first, act later"
    - "Always get explicit user confirmation before submitting a pact"
    - "present a 4-item preview (Intent, Execution Plan, Policies, Completion Conditions)"
    - Minimum-scope principle: never request more scope than the task requires

    维度:
      - pact_proposal_quality (30%): 4-item preview 是否呈现 (Intent/Plan/Policies/Completion)
      - scope_correctness     (30%): pact scope 正确（chain/token/amount 策略，最小权限）
      - pact_submitted        (25%): 实际调用了 caw pact submit
      - confirmation_obtained (15%): 提案后等待用户确认再执行
    """
    hints = expected.get("pact_hints", {})
    operation_type = hints.get("operation_type", "")
    should_refuse = hints.get("should_refuse", False)
    text_lower = content.lower()

    # pact_proposal_quality: 4-item checklist
    preview_items = {
        "intent":      ["意图", "intent", "操作", "operation", "transfer", "swap", "存", "deposit"],
        "plan":        ["执行计划", "execution plan", "步骤", "step", "1.", "approve", "plan"],
        "policies":    ["策略", "policies", "policy", "限制", "limit", "chain", "token", "amount"],
        "completion":  ["完成条件", "completion", "成功条件", "success criteria", "退出", "done"],
    }
    covered = sum(
        1 for keywords in preview_items.values()
        if any(kw.lower() in text_lower for kw in keywords)
    )
    preview_score = round(covered / 4 * 9 + 1, 1)

    # scope_correctness
    scope_signals = ["chain", "token", "amount", "limit", "policy", "permission",
                     "策略", "链", "代币", "限额"]
    found_scope = sum(1 for s in scope_signals if s in text_lower)
    # Operation-specific checks
    token_val = hints.get("token", hints.get("token_in", ""))
    amount_val = hints.get("amount", hints.get("amount_in", ""))
    chain_val = hints.get("chain", metadata.get("chain", ""))
    param_found = sum(
        1 for v in [token_val, amount_val, chain_val]
        if v and v.lower() not in ("multi", "") and v.lower() in text_lower
    )
    scope_score = min(10.0, 2.0 + found_scope * 0.8 + param_found * 1.5)

    # pact_submitted
    pact_submit_signals = ["caw pact submit", "caw pact create", "pact submit", "pact_id",
                           "pact submitted", "pact created", "already have a pact"]
    submitted = any(s.lower() in text_lower for s in pact_submit_signals)
    if should_refuse:
        # Refused correctly → pact should NOT be submitted
        submit_score = 10.0 if not submitted else 1.0
    else:
        submit_score = 10.0 if submitted else 3.0

    # confirmation_obtained: did agent present proposal and wait?
    confirm_signals = ["确认", "confirm", "shall i", "is this correct", "proceed",
                       "请确认", "是否", "approve this", "would you like"]
    confirmed = any(s.lower() in text_lower for s in confirm_signals)
    confirm_score = 9.0 if confirmed else 5.0

    weights = {"preview": 0.30, "scope": 0.30, "submit": 0.25, "confirm": 0.15}
    weighted = (
        preview_score * weights["preview"]
        + scope_score * weights["scope"]
        + submit_score * weights["submit"]
        + confirm_score * weights["confirm"]
    )
    return {
        "stage": "S2",
        "pact_proposal_quality": round(preview_score, 1),
        "scope_correctness": round(scope_score, 1),
        "pact_submitted": round(submit_score, 1),
        "confirmation_obtained": round(confirm_score, 1),
        "stage_score": round(weighted, 2),
    }


def score_s3_execution(content: str, expected: dict, metadata: dict) -> dict[str, Any]:
    """
    S3 交易执行 (权重 40%)

    评估 agent 是否使用正确命令、参数执行交易，正确处理 denial/error，
    并验证/汇报链上执行结果。
    依据 SKILL.md: caw tx transfer <pact-id> / caw tx call <pact-id>；
    Sequential execution for same-address transactions；
    When denied → report suggestion, do not improvise；
    "Always verify with caw pact show or caw tx get before retrying";
    pending_approval (HTTP 202) → poll with caw pending get, not an error。

    维度:
      - command_selection    (30%): 正确选择 caw tx transfer vs caw tx call
      - parameter_accuracy   (35%): --to/--amount/--token-id/--chain-id/--pact-id/--request-id 正确
      - error_handling       (20%): denial/failure 按规范处理（报告 suggestion，不越权重试）
      - result_verification  (15%): 报告了 tx ID/status/amount；pending_approval 正确处理
    """
    hints = expected.get("pact_hints", {})
    operation_type = hints.get("operation_type", "")
    should_refuse = hints.get("should_refuse", False)
    text_lower = content.lower()

    # command_selection
    tool_map = {
        "transfer": ["caw tx transfer", "caw transfer"],
        "swap":     ["caw tx call", "exactinputsingle", "caw swap"],
        "lend":     ["caw tx call", "aave", "supply", "caw deposit"],
        "bridge":   ["caw tx call", "caw bridge"],
        "dca":      ["caw tx call", "caw dca", "recurring"],
        "query":    ["caw wallet balance", "caw balance", "caw status", "caw tx list"],
        "multi_step": ["caw tx"],
    }
    expected_tools = tool_map.get(operation_type, ["caw"])
    found_tools = [t for t in expected_tools if t.lower() in text_lower]
    if should_refuse:
        cmd_score = 10.0 if not found_tools else 1.0
    else:
        cmd_score = 10.0 if found_tools else 3.0

    # parameter_accuracy
    param_hints = {
        "token": hints.get("token", hints.get("token_in", "")),
        "amount": hints.get("amount", hints.get("amount_in", "")),
        "chain": hints.get("chain", metadata.get("chain", "")),
    }
    param_checks = [
        v.lower() in text_lower
        for v in param_hints.values()
        if v and v.lower() not in ("multi", "")
    ]
    has_pact_id = any(s in text_lower for s in ("pact-id", "pact_id", "--pact", "under pact"))
    param_score = (sum(param_checks) / max(len(param_checks), 1)) * 8 + 1 if param_checks else 5.0
    if has_pact_id and not should_refuse:
        param_score = min(10.0, param_score + 1.0)

    # error_handling
    denial_signals = ["denied", "拒绝", "policy", "suggestion", "retry", "limit exceeded",
                      "insufficient", "余额不足", "failed", "失败"]
    has_denial_handling = any(s in text_lower for s in denial_signals)
    if should_refuse:
        err_score = 10.0 if has_denial_handling else 3.0
    elif has_denial_handling:
        recovery_signals = ["suggestion", "建议", "adjust", "retry with", "重试"]
        has_recovery = any(s in text_lower for s in recovery_signals)
        err_score = 9.0 if has_recovery else 6.0
    else:
        err_score = 9.0

    # result_verification: did the agent report tx outcome?
    report_signals = ["tx id", "txid", "transaction id", "交易 id", "status", "状态",
                      "success", "成功", "failed", "失败", "pending", "hash",
                      "amount", "金额", "caw tx get", "caw pact show"]
    found_report = sum(1 for s in report_signals if s in text_lower)
    pending_ok = "pending_approval" in text_lower or "caw pending get" in text_lower
    result_score = min(10.0, 2.0 + found_report * 1.5)
    if pending_ok:
        result_score = min(10.0, result_score + 1.0)

    weights = {"cmd": 0.30, "param": 0.35, "error": 0.20, "result": 0.15}
    weighted = (
        cmd_score * weights["cmd"]
        + param_score * weights["param"]
        + err_score * weights["error"]
        + result_score * weights["result"]
    )
    return {
        "stage": "S3",
        "command_selection": round(cmd_score, 1),
        "parameter_accuracy": round(param_score, 1),
        "error_handling": round(err_score, 1),
        "result_verification": round(result_score, 1),
        "stage_score": round(weighted, 2),
    }


def score_task_completion(full_content: str, s3_content: str, expected: dict, metadata: dict) -> dict[str, Any]:
    """
    一级核心指标：任务完成度 (0-10)

    评估 agent 是否实际完成了用户的请求，关注最终结果而非过程规范。
    区分「有验证的成功」「表观成功」「有恢复的失败」「纯失败」四个级别。
    """
    text_lower = full_content.lower()
    s3_lower = s3_content.lower()
    should_refuse = expected.get("pact_hints", {}).get("should_refuse", False)

    # Verified completion: agent showed tx ID/hash + confirmed status
    verified_signals = ["caw tx get", "caw pact show", "txid:", "tx id:", "hash:", "transaction_id",
                        "request_id", "record_uuid", "caw tx list"]
    success_signals = ["成功", "success", "completed", "complete", "confirmed", "已完成",
                       "execution complete", "执行完成", "transferred", "deposited", "swapped"]
    fail_signals = ["failed", "失败", "cannot", "无法", "error without", "execution failed",
                    "unable to", "could not"]
    recovery_signals = ["suggestion", "建议", "alternatively", "instead", "try", "retry", "重试",
                        "adjust", "adjust the"]

    verified_count = sum(1 for s in verified_signals if s in s3_lower)
    success_count = sum(1 for s in success_signals if s in text_lower)
    fail_count = sum(1 for s in fail_signals if s in text_lower)
    recovery_count = sum(1 for s in recovery_signals if s in text_lower)

    if should_refuse:
        # Expected: correctly refuse the operation
        correctly_refused = fail_count > 0 or any(
            s in text_lower for s in ["拒绝", "reject", "cannot proceed", "policy", "不支持"]
        )
        score = 9.0 if correctly_refused else 3.0
    else:
        if verified_count >= 2 and success_count >= 1:
            score = 9.5  # Verified completion: tx ID shown + status confirmed
        elif success_count >= 2 and verified_count >= 1:
            score = 8.0  # Strong apparent success with some verification
        elif success_count >= 1:
            score = 5.5  # Weak success claim, limited verification
        elif fail_count > 0 and recovery_count >= 1:
            score = 4.0  # Failed but handled gracefully with suggestion
        elif fail_count > 0:
            score = 2.0  # Failed with no recovery
        else:
            score = 4.0  # Ambiguous outcome

    return {
        "metric": "task_completion",
        "tier": "primary",
        "score": round(score, 1),
        "verified": verified_count >= 2,
    }


def score_diagnostics(full_content: str, expected: dict) -> dict[str, Any]:
    """
    三级诊断指标

    - error_type: 主要错误类型分类
        none / policy_denied / no_pact / wrong_params / wrong_command / network_error / other_error
    - recovery_logic (0-10): agent 遇到错误后的恢复逻辑质量
    - hallucination_risk (0-10): 越低代表幻觉风险越高
        检测 agent 是否声称成功但无验证证据、捏造 tx ID 等
    """
    text_lower = full_content.lower()

    # error_type classification (checked in priority order)
    error_type = "none"
    if any(s in text_lower for s in ["policy denied", "policy_denied", "denied by policy", "超出限制", "拒绝该操作"]):
        error_type = "policy_denied"
    elif any(s in text_lower for s in ["no pact", "没有 pact", "pact required", "need a pact", "需要 pact", "without a pact"]):
        error_type = "no_pact"
    elif any(s in text_lower for s in ["invalid param", "wrong param", "参数错误", "invalid argument", "invalid flag"]):
        error_type = "wrong_params"
    elif any(s in text_lower for s in ["command not found", "unknown command", "unrecognized command", "invalid command"]):
        error_type = "wrong_command"
    elif any(s in text_lower for s in ["timeout", "network error", "connection refused", "超时", "网络错误"]):
        error_type = "network_error"
    elif any(s in text_lower for s in ["failed", "失败", "error", "错误", "exception"]):
        error_type = "other_error"

    # recovery_logic (0-10)
    recovery_signals = ["suggestion", "建议", "adjust", "retry with", "alternative",
                        "alternatively", "consider", "instead", "重试", "另一种方式"]
    graceful_signals = ["unfortunately", "however", "but i can", "let me try",
                        "i wasn't able to", "让我尝试", "我无法", "不过"]
    recovery_count = sum(1 for s in recovery_signals if s in text_lower)
    graceful_count = sum(1 for s in graceful_signals if s in text_lower)

    if error_type == "none":
        recovery_score = 9.0
    elif recovery_count >= 2 and graceful_count >= 1:
        recovery_score = 8.5
    elif recovery_count >= 1:
        recovery_score = 6.5
    elif graceful_count >= 1:
        recovery_score = 4.5
    else:
        recovery_score = 2.0

    # hallucination_risk (0-10; 越低 → 风险越高)
    # 检测: 声称成功但无 tx ID/验证命令 → 潜在幻觉
    fabrication_signals = ["transaction successful", "transfer complete", "executed successfully",
                           "swap completed", "transaction confirmed", "交易成功完成", "转账完成"]
    verification_signals = ["caw tx get", "caw pact show", "txid:", "tx id:", "hash:",
                            "request_id", "status:", "caw tx list"]
    has_bare_success_claim = any(s in text_lower for s in fabrication_signals)
    has_verification = any(s in text_lower for s in verification_signals)

    if has_bare_success_claim and not has_verification:
        hallucination_risk = 3.0  # High risk: unverified success claim
    elif has_bare_success_claim and has_verification:
        hallucination_risk = 8.0  # Low risk: claimed success and verified
    else:
        hallucination_risk = 8.5  # Neutral: no strong unverified claims

    return {
        "metric": "diagnostics",
        "tier": "tertiary",
        "error_type": error_type,
        "recovery_logic": round(recovery_score, 1),
        "hallucination_risk": round(hallucination_risk, 1),
    }


def compute_e2e_score(
    stage_scores: dict[str, dict],
    task_completion: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    E2E 端到端综合评分（指标层级）

    一级 (核心): task_completion — 任务完成度
    二级 (关键): recipe_hit_quality  — Recipe/命令命中质量
                 pact_intent_match   — Pact 与意图匹配度
    三级 (诊断): error_type / recovery_logic / hallucination_risk
    """
    weighted_sum = 0.0
    for stage, weight in STAGE_WEIGHTS.items():
        score = stage_scores.get(stage, {}).get("stage_score", 5.0)
        weighted_sum += score * weight

    # 二级指标: 从现有阶段分数中派生
    recipe_hit_quality = stage_scores.get("s3", {}).get("command_selection", 5.0)
    pact_intent_match = round(
        stage_scores.get("s1", {}).get("entity_extraction", 5.0) * 0.3
        + stage_scores.get("s2", {}).get("scope_correctness", 5.0) * 0.7,
        2,
    )

    result: dict[str, Any] = {
        "stage": "E2E",
        "weighted_composite": round(weighted_sum, 2),
        "stage_breakdown": {
            stage: round(stage_scores.get(stage, {}).get("stage_score", 0.0), 2)
            for stage in STAGE_WEIGHTS
        },
        "stage_score": round(weighted_sum, 2),
        # 二级指标
        "recipe_hit_quality": round(recipe_hit_quality, 2),
        "pact_intent_match": pact_intent_match,
    }

    # 一级指标
    if task_completion is not None:
        result["task_completion"] = task_completion["score"]

    # 三级诊断
    if diagnostics is not None:
        result["error_type"] = diagnostics["error_type"]
        result["recovery_logic"] = diagnostics["recovery_logic"]
        result["hallucination_risk"] = diagnostics["hallucination_risk"]

    return result


# ── Judge Request Builder ─────────────────────────────────────────────────────

def build_judge_request(
    trace_id: str,
    expected: dict,
    metadata: dict,
    *,
    session_path: str | None = None,
    conversation_text: str | None = None,
    item_id: str = "",
) -> dict[str, Any]:
    """
    Build a self-contained judge request dict for a single trace/session.

    The resulting dict can be serialised to JSON and fed to an openclaw task
    subagent by Copilot.  The subagent should return a JSON object whose
    structure matches the expected judge_result format (see load_judge_results).
    """
    operation_type = metadata.get("operation_type", "unknown")
    difficulty = metadata.get("difficulty", "L1")
    user_msg = expected.get("user_message", "")
    success_criteria = expected.get("success_criteria", "")
    hints = expected.get("pact_hints", {})
    tags = metadata.get("tags", [])
    should_refuse = hints.get("should_refuse", False)

    system_prompt = """你是 CAW (Cobo Agentic Wallet) AI Agent 的专业评估专家。

CAW workflow 知识:
- caw pact submit: 提交最小权限 pact，必须先展示 4-item preview (Intent / Execution Plan / Policies / Completion Conditions) 并获得用户确认
- caw tx transfer <pact-id>: 原生代币/ERC-20 转账
- caw tx call <pact-id>: 合约调用（swap/lend/bridge/DCA）
- pending_approval (HTTP 202): 使用 caw pending get 轮询，不是错误
- should_refuse 场景: agent 应明确拒绝操作，不提交 pact，不执行 tx
- denial/policy 处理: 汇报 suggestion，不越权重试

评分原则:
- 各维度 1-10 分（越高越好）
- 依据 CAW skill 规范严格评分，不宽泛给分
- 必须返回合法 JSON，不要有任何额外内容"""

    pact_hints_str = json.dumps(hints, ensure_ascii=False)
    content_instruction = (
        f"请使用 Read 工具读取并分析 session 文件: {session_path}"
        if session_path
        else f"以下是 agent 对话内容（前 4000 字符）:\n{(conversation_text or '')[:4000]}"
    )

    prompt = f"""**评估任务**
操作类型: {operation_type} | 难度: {difficulty}
用户指令: {user_msg}
成功标准: {success_criteria}
pact_hints: {pact_hints_str}
tags: {json.dumps(tags, ensure_ascii=False)}
should_refuse: {should_refuse}

{content_instruction}

**评分维度**（各项 1-10 分）

S1 意图解析（阶段分 = op*0.4 + entity*0.4 + constraint*0.2）:
- operation_type_accuracy: 是否正确识别操作类型
- entity_extraction: token/amount/chain/address 是否正确提取
- constraint_recognition: 隐含约束（滑点/周期/gas 预留）是否识别

S2 Pact 协商（阶段分 = proposal*0.3 + scope*0.3 + submit*0.25 + confirm*0.15）:
- pact_proposal_quality: 是否展示了 4-item preview
- scope_correctness: 权限范围是否正确（chain/token/amount 最小权限）
- pact_submitted: caw pact submit 是否实际被调用（should_refuse=true 时不应提交）
- confirmation_obtained: 提交前是否获得用户确认

S3 交易执行（阶段分 = cmd*0.3 + param*0.35 + error*0.2 + result*0.15）:
- command_selection: 是否选择了正确的 caw 命令
- parameter_accuracy: 参数是否正确
- error_handling: denial/policy/error 处理是否规范
- result_verification: 是否汇报了 tx ID/状态

综合指标:
- task_completion (1-10): 任务是否实际完成
- recipe_hit_quality (1-10): 命令/Recipe 选择质量
- pact_intent_match (1-10): pact scope 与用户意图匹配度
- error_type: "none"/"policy_denied"/"no_pact"/"wrong_params"/"wrong_command"/"network_error"/"other_error"
- recovery_logic (1-10): 错误恢复逻辑质量
- hallucination_risk (1-10): 越低代表幻觉风险越高
- e2e: S1*0.2 + S2*0.4 + S3*0.4
- comment: 不超过 50 字的综合评语

以合法 JSON 返回（不要有任何其他内容）:
{{
  "s1": {{"operation_type_accuracy": X, "entity_extraction": X, "constraint_recognition": X, "stage_score": X, "stage": "S1", "note": "..."}},
  "s2": {{"pact_proposal_quality": X, "scope_correctness": X, "pact_submitted": X, "confirmation_obtained": X, "stage_score": X, "stage": "S2", "note": "..."}},
  "s3": {{"command_selection": X, "parameter_accuracy": X, "error_handling": X, "result_verification": X, "stage_score": X, "stage": "S3", "note": "..."}},
  "task_completion": X,
  "recipe_hit_quality": X,
  "pact_intent_match": X,
  "error_type": "none",
  "recovery_logic": X,
  "hallucination_risk": X,
  "e2e": X,
  "comment": "..."
}}"""

    return {
        "trace_id": trace_id,
        "item_id": item_id,
        "metadata": metadata,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "session_path": session_path,
    }


def load_judge_results(path: str) -> dict[str, dict[str, Any]]:
    """
    Load a judge results JSON file and return a {trace_id: result} mapping.

    The file is expected to be a JSON array of objects, each with a "trace_id"
    field plus the S1/S2/S3 scoring fields produced by the subagent.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {entry["trace_id"]: {**entry, "available": True} for entry in raw if "trace_id" in entry}
    raise ValueError(f"judge results file must be a JSON array, got {type(raw).__name__}")


# ── Main scoring orchestrator ─────────────────────────────────────────────────

def score_trace_full(
    trace_id: str,
    lf: Any,
    item_input: dict,
    item_expected: dict,
    item_metadata: dict,
    dry_run: bool = False,
    judge_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    对单个 trace 进行完整 S1-S3 + E2E 评分。

    流程:
      1. 从 Langfuse 拉取 trace 及其 observations
      2. 提取各阶段相关文本
      3. 使用预计算的 judge_result（由 Copilot task subagent 生成）或退回启发式评分
      4. 计算 E2E 加权综合分
      5. 通过 Langfuse SDK 创建新的 scoring trace
      6. 通过 Langfuse SDK 将各维度分数上传到原始 trace
    """
    print(f"  → {trace_id[:16]}...")

    # 1. Fetch trace from Langfuse (read via SDK — no backend proxy for trace details)
    try:
        trace = lf.api.trace.get(trace_id=trace_id)
    except Exception as e:
        print(f"    [ERROR] Cannot fetch trace: {e}")
        return {"error": str(e), "trace_id": trace_id}

    # 2. Extract stage content
    stage_content = extract_stage_content(trace)
    if not stage_content["full"].strip():
        print("    [WARN] Empty trace")
        return {"skipped": True, "trace_id": trace_id}

    expected_with_msg = {
        **item_expected,
        "user_message": item_input.get("user_message", ""),
    }

    # 3-4. Use pre-computed judge result (primary) or fall back to heuristics
    if judge_result and judge_result.get("available"):
        s1 = judge_result["s1"]
        s2 = judge_result["s2"]
        s3 = judge_result["s3"]
        task_completion = {
            "score": float(judge_result["task_completion"]),
            "method": "subagent",
            "tier": "primary",
            "metric": "task_completion",
        }
        diagnostics = {
            "metric": "diagnostics",
            "tier": "tertiary",
            "error_type": judge_result.get("error_type", "none"),
            "recovery_logic": float(judge_result.get("recovery_logic", 9.0)),
            "hallucination_risk": float(judge_result.get("hallucination_risk", 8.5)),
        }
        scoring_source = "subagent"
    else:
        s1 = score_s1_intent_parsing(stage_content["s1"], expected_with_msg, item_metadata)
        s2 = score_s2_pact_negotiation(stage_content["s2"], expected_with_msg, item_metadata)
        s3 = score_s3_execution(stage_content["s3"], expected_with_msg, item_metadata)
        task_completion = score_task_completion(
            stage_content["full"], stage_content["s3"], expected_with_msg, item_metadata
        )
        diagnostics = score_diagnostics(stage_content["full"], expected_with_msg)
        scoring_source = "heuristic"

    stage_scores = {"s1": s1, "s2": s2, "s3": s3}
    e2e = compute_e2e_score(stage_scores, task_completion=task_completion, diagnostics=diagnostics)
    if judge_result and judge_result.get("available"):
        e2e["recipe_hit_quality"] = round(float(judge_result.get("recipe_hit_quality", e2e["recipe_hit_quality"])), 2)
        e2e["pact_intent_match"] = round(float(judge_result.get("pact_intent_match", e2e["pact_intent_match"])), 2)

    result = {
        "trace_id": trace_id,
        "scoring_source": scoring_source,
        "item_metadata": item_metadata,
        "stage_scores": stage_scores,
        "task_completion": task_completion,
        "diagnostics": diagnostics,
        "e2e": e2e,
    }

    _print_stage_summary(result)

    if dry_run:
        return result

    # 5. Create scoring trace via Langfuse SDK
    scoring_trace_id = _create_scoring_trace(lf, trace_id, result, item_metadata)
    result["scoring_trace_id"] = scoring_trace_id

    # 6. Upload scores to original trace via Langfuse SDK
    _upload_scores_to_trace(lf, trace_id, stage_scores, e2e, task_completion, diagnostics, scoring_source)
    lf.flush()

    return result


def score_session_file(
    session_path: str,
    item_input: dict,
    item_expected: dict,
    item_metadata: dict,
    dry_run: bool = False,
    lf: Any = None,
    judge_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    对本地 session .jsonl 文件进行完整 S1-S3 + E2E 评分。
    不需要从 Langfuse 读取 trace，直接从本地文件提取各阶段内容。
    session["session_id"] 与 Langfuse trace_id 一致（run_eval.py 上传时已关联）。
    judge_result: 由 Copilot task subagent 生成的预计算评分结果；None 时退回启发式评分。
    """
    import pathlib
    session = _parse_session_file(session_path)
    trace_id = session["session_id"]
    if not trace_id:
        raise ValueError(f"No session_id found in {session_path}")

    print(f"  → session {trace_id[:16]}... ({pathlib.Path(session_path).name})")

    stage_content = extract_stage_content_from_session(session)
    if not stage_content["full"].strip():
        print("    [WARN] Empty session")
        return {"skipped": True, "trace_id": trace_id, "session_path": session_path}

    expected_with_msg = {
        **item_expected,
        "user_message": item_input.get("user_message", ""),
    }

    if judge_result and judge_result.get("available"):
        s1 = judge_result["s1"]
        s2 = judge_result["s2"]
        s3 = judge_result["s3"]
        task_completion = {
            "score": float(judge_result["task_completion"]),
            "method": "subagent",
            "tier": "primary",
            "metric": "task_completion",
        }
        diagnostics = {
            "metric": "diagnostics",
            "tier": "tertiary",
            "error_type": judge_result.get("error_type", "none"),
            "recovery_logic": float(judge_result.get("recovery_logic", 9.0)),
            "hallucination_risk": float(judge_result.get("hallucination_risk", 8.5)),
        }
        scoring_source = "subagent"
    else:
        s1 = score_s1_intent_parsing(stage_content["s1"], expected_with_msg, item_metadata)
        s2 = score_s2_pact_negotiation(stage_content["s2"], expected_with_msg, item_metadata)
        s3 = score_s3_execution(stage_content["s3"], expected_with_msg, item_metadata)
        task_completion = score_task_completion(
            stage_content["full"], stage_content["s3"], expected_with_msg, item_metadata
        )
        diagnostics = score_diagnostics(stage_content["full"], expected_with_msg)
        scoring_source = "heuristic"

    stage_scores = {"s1": s1, "s2": s2, "s3": s3}
    e2e = compute_e2e_score(stage_scores, task_completion=task_completion, diagnostics=diagnostics)
    if judge_result and judge_result.get("available"):
        e2e["recipe_hit_quality"] = round(float(judge_result.get("recipe_hit_quality", e2e["recipe_hit_quality"])), 2)
        e2e["pact_intent_match"] = round(float(judge_result.get("pact_intent_match", e2e["pact_intent_match"])), 2)

    result = {
        "trace_id": trace_id,
        "session_path": session_path,
        "scoring_source": scoring_source,
        "item_metadata": item_metadata,
        "stage_scores": stage_scores,
        "task_completion": task_completion,
        "diagnostics": diagnostics,
        "e2e": e2e,
    }

    _print_stage_summary(result)

    if dry_run:
        return result

    _lf = lf or _make_langfuse()
    scoring_trace_id = _create_scoring_trace(_lf, trace_id, result, item_metadata)
    result["scoring_trace_id"] = scoring_trace_id
    _upload_scores_to_trace(_lf, trace_id, stage_scores, e2e, task_completion, diagnostics, scoring_source)
    _lf.flush()

    return result


def _print_stage_summary(result: dict) -> None:
    ss = result["stage_scores"]
    e2e = result["e2e"]
    tc = result.get("task_completion", {})
    diag = result.get("diagnostics", {})
    source = result.get("scoring_source", "heuristic")
    source_tag = "[subagent]" if source == "subagent" else "[heuristic]"
    tc_str = f"{tc['score']:.1f}" if tc.get("score") is not None else "N/A"
    err_str = diag.get("error_type", "")
    print(
        f"    [一级] 任务完成度={tc_str}  "
        f"[二级] Recipe={e2e.get('recipe_hit_quality', 0):.1f} Pact匹配={e2e.get('pact_intent_match', 0):.1f}\n"
        f"    [三级] 错误类型={err_str or 'none'} 恢复={diag.get('recovery_logic', '-')} 幻觉风险={diag.get('hallucination_risk', '-')}\n"
        f"    S1={ss['s1']['stage_score']:.1f} S2={ss['s2']['stage_score']:.1f} S3={ss['s3']['stage_score']:.1f} "
        f"{source_tag} → E2E={e2e['stage_score']:.2f}"
    )


def _create_scoring_trace(
    lf: Any,
    original_trace_id: str,
    result: dict,
    metadata: dict,
) -> str | None:
    """
    通过 Langfuse v4 ingestion API 在 Langfuse 中创建评分 trace。
    input 指向原始 trace_id，output 包含分阶段评分结果。
    返回新建 trace 的 trace_id。
    """
    import uuid as _uuid
    from datetime import datetime, timezone
    try:
        from langfuse.api import TraceBody, IngestionEvent_TraceCreate
    except ImportError:
        print("    [SCORING TRACE ERROR] Cannot import Langfuse v4 ingestion types")
        return None

    try:
        scoring_trace_id = str(_uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()
        trace_body = TraceBody(
            id=scoring_trace_id,
            name="caw-eval-scoring",
            session_id=original_trace_id,
            timestamp=now_iso,
            input={
                "original_trace_id": original_trace_id,
                "operation_type": metadata.get("operation_type", ""),
                "difficulty": metadata.get("difficulty", ""),
                "chain": metadata.get("chain", ""),
            },
            output={
                "stage_scores": {
                    k: {"score": v["stage_score"], "details": v}
                    for k, v in result["stage_scores"].items()
                },
                "e2e": result["e2e"],
                "scoring_source": result.get("scoring_source", "heuristic"),
            },
            metadata={"eval": "true", "evaluated_trace_id": original_trace_id},
        )
        event = IngestionEvent_TraceCreate(
            id=str(_uuid.uuid4()),
            timestamp=now_iso,
            body=trace_body,
        )
        lf.api.ingestion.batch(batch=[event])
        print(f"    [SCORING TRACE] {scoring_trace_id}")
        return scoring_trace_id
    except Exception as e:
        print(f"    [SCORING TRACE ERROR] {e}")
        return None


def _upload_scores_to_trace(
    lf: Any,
    trace_id: str,
    stage_scores: dict,
    e2e: dict,
    task_completion: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
    scoring_source: str = "heuristic",
) -> None:
    """通过 Langfuse SDK 将各阶段分数上传到原始 trace。"""
    scores_to_upload: list[tuple[str, float, str]] = [
        ("caw.s1_intent", stage_scores["s1"]["stage_score"], f"S1 意图解析 ({scoring_source})"),
        ("caw.s2_pact", stage_scores["s2"]["stage_score"], f"S2 Pact 协商 ({scoring_source})"),
        ("caw.s3_execution", stage_scores["s3"]["stage_score"], f"S3 交易执行 ({scoring_source})"),
        ("caw.e2e_composite", e2e["stage_score"], f"E2E 综合 (加权) ({scoring_source})"),
        # 二级指标
        ("caw.recipe_hit_quality", e2e.get("recipe_hit_quality", 0.0), "Recipe 命中质量"),
        ("caw.pact_intent_match", e2e.get("pact_intent_match", 0.0), "Pact 与意图匹配度"),
        # 评分来源标记
        ("caw.scoring_source", 1.0 if scoring_source == "subagent" else 0.0,
         f"scoring_source={scoring_source}"),
    ]
    # 一级核心指标
    if task_completion and task_completion.get("score") is not None:
        scores_to_upload.append(("caw.task_completion", task_completion["score"], "任务完成度 (核心)"))
    # 三级诊断
    if diagnostics:
        scores_to_upload.append(
            ("caw.recovery_logic", diagnostics["recovery_logic"],
             f"恢复逻辑 | error_type={diagnostics['error_type']}")
        )
        scores_to_upload.append(
            ("caw.hallucination_risk", diagnostics["hallucination_risk"], "幻觉风险 (越低风险越高)")
        )

    for name, value, comment in scores_to_upload:
        try:
            lf.create_score(trace_id=trace_id, name=name, value=float(value), comment=comment or "")
        except Exception as e:
            print(f"    [SCORE UPLOAD ERROR] {name}: {e}")


# ── Dataset run scoring ───────────────────────────────────────────────────────

def score_dataset_run(
    dataset_name: str,
    run_name: str,
    lf: Any,
    dry_run: bool = False,
    judge_results_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict]:
    print(f"[INFO] Loading '{dataset_name}' / run '{run_name}' ...")
    try:
        dataset = lf.get_dataset(dataset_name)
        run = lf.get_dataset_run(dataset_name=dataset_name, run_name=run_name)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    item_map = {item.id: item for item in dataset.items}
    run_items = getattr(run, "dataset_run_items", []) or []
    print(f"[INFO] {len(run_items)} run items to score")

    results: list[dict] = []
    for run_item in run_items:
        trace_id = getattr(run_item, "trace_id", None)
        item_id = getattr(run_item, "dataset_item_id", None)
        if not trace_id:
            continue

        item = item_map.get(item_id)
        if not item:
            print(f"  [SKIP] item={item_id} not found")
            continue

        print(f"\n  [{item_id}] {item.metadata.get('difficulty','?')} | "
              f"{item.metadata.get('operation_type','?')}")

        result = score_trace_full(
            trace_id=trace_id,
            lf=lf,
            item_input=item.input or {},
            item_expected=item.expected_output or {},
            item_metadata=item.metadata or {},
            dry_run=dry_run,
            judge_result=(judge_results_map or {}).get(trace_id),
        )
        result["item_id"] = item_id
        results.append(result)

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[dict], run_name: str) -> None:
    valid = [r for r in results if "stage_scores" in r]
    if not valid:
        print("No valid results")
        return

    def avg_stage(key: str) -> float:
        vals = [r["stage_scores"][key]["stage_score"] for r in valid if key in r.get("stage_scores", {})]
        return sum(vals) / len(vals) if vals else 0.0

    def avg_e2e_field(field: str) -> float:
        vals = [r["e2e"][field] for r in valid if field in r.get("e2e", {})]
        return sum(vals) / len(vals) if vals else 0.0

    def avg_tc() -> float:
        vals = [r["task_completion"]["score"] for r in valid if r.get("task_completion", {}).get("score") is not None]
        return sum(vals) / len(vals) if vals else 0.0

    e2e_avg = sum(r["e2e"]["stage_score"] for r in valid) / len(valid)

    print(f"\n{'='*70}")
    print(f"Eval Report: {run_name}   ({len(valid)} items)")
    print(f"{'='*70}")
    print(f"  ★ 一级 · 任务完成度        : {avg_tc():5.2f}")
    print(f"  ◆ 二级 · Recipe 命中质量   : {avg_e2e_field('recipe_hit_quality'):5.2f}")
    print(f"  ◆ 二级 · Pact 与意图匹配度 : {avg_e2e_field('pact_intent_match'):5.2f}")
    print(f"  ─ 分析 · S1 意图解析 (20%) : {avg_stage('s1'):5.2f}")
    print(f"  ─ 分析 · S2 Pact 协商 (40%): {avg_stage('s2'):5.2f}")
    print(f"  ─ 分析 · S3 交易执行 (40%) : {avg_stage('s3'):5.2f}")
    print(f"  ─ 综合 · E2E Composite     : {e2e_avg:5.2f}")

    # Error type distribution
    error_counts: dict[str, int] = {}
    for r in valid:
        et = r.get("diagnostics", {}).get("error_type", "none")
        error_counts[et] = error_counts.get(et, 0) + 1
    if error_counts:
        dist = "  ".join(f"{k}={v}" for k, v in sorted(error_counts.items()))
        print(f"  ◇ 三级 · 错误类型分布      : {dist}")

    diag_vals = [r.get("diagnostics", {}) for r in valid if r.get("diagnostics")]
    if diag_vals:
        avg_rec = sum(d.get("recovery_logic", 0) for d in diag_vals) / len(diag_vals)
        avg_hal = sum(d.get("hallucination_risk", 0) for d in diag_vals) / len(diag_vals)
        print(f"  ◇ 三级 · 恢复逻辑均值      : {avg_rec:5.2f}")
        print(f"  ◇ 三级 · 幻觉风险均值      : {avg_hal:5.2f}")
    print()

    header = f"{'Item':<14} {'完成度':>6} {'Recipe':>7} {'Pact匹配':>8} {'S1':>5} {'S2':>5} {'S3':>5} {'E2E':>6} {'错误类型':<14}"
    print(header)
    print("-" * len(header))
    for r in valid:
        ss = r["stage_scores"]
        e2e = r["e2e"]
        tc = r.get("task_completion", {}).get("score", 0.0)
        diag = r.get("diagnostics", {})
        item_id = r.get("item_id", "?")
        print(
            f"{item_id:<14} "
            f"{tc:>6.1f} "
            f"{e2e.get('recipe_hit_quality', 0):>7.1f} "
            f"{e2e.get('pact_intent_match', 0):>8.1f} "
            f"{ss['s1']['stage_score']:>5.1f} "
            f"{ss['s2']['stage_score']:>5.1f} "
            f"{ss['s3']['stage_score']:>5.1f} "
            f"{e2e['stage_score']:>6.2f} "
            f"{diag.get('error_type', 'none'):<14}"
        )


def session_main() -> None:
    """
    Subcommand: score one or more local session .jsonl files.

    用法:
        # 阶段一：生成 judge prompt 文件
        python score_traces.py session --session /path/to/sessions_dir/ --dump-judge-requests /tmp/req.json

        # 阶段三：使用 Copilot task subagent 生成的评分结果
        python score_traces.py session --session /path/to/sessions_dir/ --judge-results /tmp/results.json

        # 启发式评分（不需要 subagent）
        python score_traces.py session --session /path/to/session.jsonl --report
        python score_traces.py session --session session.jsonl --item-id E2E-01L1 --dataset-name caw-agent-eval-v1
    """
    import pathlib

    parser = argparse.ArgumentParser(
        prog="score_traces.py session",
        description="Score local session .jsonl files without reading from Langfuse.",
    )
    parser.add_argument("--session", required=True,
                        help="Path to a session .jsonl file, or directory containing .jsonl files")
    parser.add_argument("--item-id",
                        help="Dataset item ID (e.g. E2E-01L1). If set, fetches item context from Langfuse dataset.")
    parser.add_argument("--dataset-name", default="caw-agent-eval-v1",
                        help="Dataset name to look up --item-id [default: caw-agent-eval-v1]")
    parser.add_argument("--dry-run", action="store_true", help="Score without uploading to Langfuse")
    parser.add_argument("--report", action="store_true", help="Print summary table after scoring")
    parser.add_argument("--output", help="Save results JSON to file")
    parser.add_argument("--dump-judge-requests", metavar="FILE",
                        help="Write judge prompt requests to FILE and exit (phase 1 of subagent scoring)")
    parser.add_argument("--judge-results", metavar="FILE",
                        help="Read pre-computed judge results from FILE (phase 3 of subagent scoring)")
    args = parser.parse_args(sys.argv[2:])

    lf = None if (args.dry_run or args.dump_judge_requests) else _make_langfuse()

    # Optionally fetch item context from Langfuse dataset project
    item_input: dict = {}
    item_expected: dict = {}
    item_metadata: dict = {}
    if args.item_id:
        try:
            lf_ds = _make_dataset_langfuse()
            dataset = lf_ds.get_dataset(args.dataset_name)
            matching = [i for i in dataset.items if i.id == args.item_id]
            if matching:
                item = matching[0]
                item_input = item.input or {}
                item_expected = item.expected_output or {}
                item_metadata = item.metadata or {}
                print(f"[INFO] Loaded item context: {args.item_id} "
                      f"({item_metadata.get('operation_type', '?')} / "
                      f"{item_metadata.get('difficulty', '?')})")
            else:
                print(f"[WARN] Item {args.item_id!r} not found in dataset {args.dataset_name!r}. "
                      "Scoring without item context.")
        except Exception as e:
            print(f"[WARN] Failed to fetch item context for {args.item_id!r}: {e}. "
                  "Scoring without item context.")

    session_path = pathlib.Path(args.session)
    if session_path.is_dir():
        session_files = sorted(session_path.glob("*.jsonl"))
    else:
        session_files = [session_path]

    if not session_files:
        print(f"[ERROR] No .jsonl files found at {args.session}", file=sys.stderr)
        sys.exit(1)

    if not args.item_id and not item_input:
        print("[WARN] No --item-id provided. Scoring without expected output / metadata context. "
              "Use --item-id <E2E-XXX> to load item-specific scoring criteria from the dataset.")

    # ── Phase 1: dump judge requests ──────────────────────────────────────────
    if args.dump_judge_requests:
        requests: list[dict] = []
        for sf in session_files:
            try:
                session = _parse_session_file(str(sf))
                trace_id = session["session_id"] or sf.stem
                stage_content = extract_stage_content_from_session(session)
                expected_with_msg = {**item_expected, "user_message": item_input.get("user_message", "")}
                req = build_judge_request(
                    trace_id=trace_id,
                    expected=expected_with_msg,
                    metadata=item_metadata or {"session_file": sf.name},
                    session_path=str(sf),
                    conversation_text=stage_content["full"],
                    item_id=args.item_id or "",
                )
                requests.append(req)
            except Exception as e:
                print(f"  [ERROR] {sf.name}: {e}")
        Path(args.dump_judge_requests).write_text(json.dumps(requests, indent=2, ensure_ascii=False))
        print(f"[SAVED] {len(requests)} judge request(s) → {args.dump_judge_requests}")
        print("[NEXT] Run Copilot task subagent for each request, then re-run with --judge-results <file>")
        return

    # ── Phase 3: load pre-computed judge results ──────────────────────────────
    judge_results_map: dict[str, dict] = {}
    if args.judge_results:
        judge_results_map = load_judge_results(args.judge_results)
        print(f"[INFO] Loaded {len(judge_results_map)} judge result(s) from {args.judge_results}")
    else:
        print("[INFO] No --judge-results provided — using heuristic scoring")

    print(f"[INFO] Scoring {len(session_files)} session file(s)...")
    results: list[dict] = []
    for sf in session_files:
        try:
            session = _parse_session_file(str(sf))
            trace_id = session.get("session_id") or sf.stem
            result = score_session_file(
                str(sf),
                item_input=item_input,
                item_expected=item_expected,
                item_metadata=item_metadata or {"session_file": sf.name},
                dry_run=args.dry_run,
                lf=lf,
                judge_result=judge_results_map.get(trace_id),
            )
            results.append(result)
        except Exception as e:
            print(f"  [ERROR] {sf.name}: {e}")

    if args.report or len(session_files) > 1:
        print_report(results, args.session)

    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"[SAVED] {args.output}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Dispatch to subcommands
    if len(sys.argv) > 1 and sys.argv[1] == "session":
        session_main()
        return

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", default="caw-agent-eval-v1")
    parser.add_argument("--run-name", help="Dataset run name to score")
    parser.add_argument("--trace-id", help="Score a single trace by ID")
    parser.add_argument("--report", action="store_true", help="Print summary table")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score without uploading to Langfuse")
    parser.add_argument("--output", help="Save results JSON to file")
    parser.add_argument("--dump-judge-requests", metavar="FILE",
                        help="Write judge prompt requests to FILE and exit (phase 1 of subagent scoring)")
    parser.add_argument("--judge-results", metavar="FILE",
                        help="Read pre-computed judge results from FILE (phase 3 of subagent scoring)")
    args = parser.parse_args()

    lf = None if (args.dry_run or args.dump_judge_requests) else _make_langfuse()

    # ── Phase 1: dump judge requests ──────────────────────────────────────────
    if args.dump_judge_requests:
        if not (args.trace_id or args.run_name):
            print("[ERROR] --dump-judge-requests requires --trace-id or --run-name", file=sys.stderr)
            sys.exit(1)
        _lf = _make_langfuse()
        requests: list[dict] = []
        if args.trace_id:
            try:
                trace = _lf.api.trace.get(trace_id=args.trace_id)
                stage_content = extract_stage_content(trace)
                req = build_judge_request(
                    trace_id=args.trace_id,
                    expected={},
                    metadata={},
                    conversation_text=stage_content["full"],
                )
                requests.append(req)
            except Exception as e:
                print(f"[ERROR] Cannot fetch trace {args.trace_id}: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                dataset = _lf.get_dataset(args.dataset_name)
                run = _lf.get_dataset_run(dataset_name=args.dataset_name, run_name=args.run_name)
            except Exception as e:
                print(f"[ERROR] {e}", file=sys.stderr)
                sys.exit(1)
            item_map = {item.id: item for item in dataset.items}
            for run_item in getattr(run, "dataset_run_items", []) or []:
                trace_id = getattr(run_item, "trace_id", None)
                item_id = getattr(run_item, "dataset_item_id", None)
                if not trace_id:
                    continue
                item = item_map.get(item_id)
                item_input = item.input or {} if item else {}
                item_expected = item.expected_output or {} if item else {}
                item_metadata = item.metadata or {} if item else {}
                try:
                    trace = _lf.api.trace.get(trace_id=trace_id)
                    stage_content = extract_stage_content(trace)
                    expected_with_msg = {**item_expected, "user_message": item_input.get("user_message", "")}
                    req = build_judge_request(
                        trace_id=trace_id,
                        expected=expected_with_msg,
                        metadata=item_metadata,
                        conversation_text=stage_content["full"],
                        item_id=item_id or "",
                    )
                    requests.append(req)
                except Exception as e:
                    print(f"  [ERROR] trace {trace_id}: {e}")
        Path(args.dump_judge_requests).write_text(json.dumps(requests, indent=2, ensure_ascii=False))
        print(f"[SAVED] {len(requests)} judge request(s) → {args.dump_judge_requests}")
        print("[NEXT] Run Copilot task subagent for each request, then re-run with --judge-results <file>")
        return

    # ── Phase 3: load pre-computed judge results ──────────────────────────────
    judge_results_map: dict[str, dict] = {}
    if args.judge_results:
        judge_results_map = load_judge_results(args.judge_results)
        print(f"[INFO] Loaded {len(judge_results_map)} judge result(s) from {args.judge_results}")
    else:
        print("[INFO] No --judge-results provided — using heuristic scoring")

    if lf is None and not args.dry_run:
        lf = _make_langfuse()

    if args.trace_id:
        if lf is None:
            lf = _make_langfuse()
        result = score_trace_full(
            trace_id=args.trace_id,
            lf=lf,
            item_input={},
            item_expected={},
            item_metadata={},
            dry_run=args.dry_run,
            judge_result=judge_results_map.get(args.trace_id),
        )
        results = [result]
    elif args.run_name:
        if lf is None:
            lf = _make_langfuse()
        results = score_dataset_run(
            args.dataset_name, args.run_name, lf, args.dry_run,
            judge_results_map=judge_results_map,
        )
    else:
        parser.print_help()
        sys.exit(1)

    if args.report or args.run_name:
        print_report(results, args.run_name or args.trace_id or "")

    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"[SAVED] {args.output}")


if __name__ == "__main__":
    main()
