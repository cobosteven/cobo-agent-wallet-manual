#!/usr/bin/env python3
"""
Script 3: 从 Langfuse 拉取 trace 数据，按 S1-S3 各阶段评分，结果直接写回 Langfuse

用法:
    # 对整个 dataset run 评分
    python score_traces.py --dataset-name caw-agent-eval-v1 --run-name eval-run-20250101

    # 对单个 trace 评分（测试/调试）
    python score_traces.py --trace-id <trace_id>

    # 生成评分报告（不上传到 Langfuse）
    python score_traces.py --run-name X --report --dry-run

    # 直接从本地 session .jsonl 文件评分（带 item 上下文）
    python score_traces.py session --session /path/to/session.jsonl \
        --item-id E2E-01L1 --dataset-name caw-agent-eval-v1
    python score_traces.py session --session /path/to/sessions_dir/ --report

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
    ANTHROPIC_API_KEY      - Claude API key（用于 LLM-as-Judge，可选）
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

_DEFAULT_RESULT_HOST = "https://langfuse.1cobo.com"
_DEFAULT_DATASET_HOST = "https://langfuse.1cobo.com"


# ── Langfuse client helper ────────────────────────────────────────────────────

def _make_langfuse() -> Any:
    """Create a Langfuse client for the *results* project.

    Priority: LANGFUSE_RESULT_* → LANGFUSE_* → hard-coded default host.
    """
    from langfuse import Langfuse

    def _pick(specific: str, generic: str, default: str = "") -> str:
        return os.environ.get(specific) or os.environ.get(generic) or default

    host = _pick("LANGFUSE_RESULT_HOST", "LANGFUSE_HOST", _DEFAULT_RESULT_HOST)
    public_key = _pick("LANGFUSE_RESULT_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    secret_key = _pick("LANGFUSE_RESULT_SECRET_KEY", "LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        print("[WARN] Langfuse results-project credentials not set. "
              "Set LANGFUSE_RESULT_PUBLIC_KEY + LANGFUSE_RESULT_SECRET_KEY "
              "(or LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY).")

    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


def _make_dataset_langfuse() -> Any:
    """Create a Langfuse client for the *dataset* project (read item context).

    Priority: LANGFUSE_DATASET_* → LANGFUSE_* → hard-coded default host.
    """
    from langfuse import Langfuse

    def _pick(specific: str, generic: str, default: str = "") -> str:
        return os.environ.get(specific) or os.environ.get(generic) or default

    host = _pick("LANGFUSE_DATASET_HOST", "LANGFUSE_HOST", _DEFAULT_DATASET_HOST)
    public_key = _pick("LANGFUSE_DATASET_PUBLIC_KEY", "LANGFUSE_PUBLIC_KEY")
    secret_key = _pick("LANGFUSE_DATASET_SECRET_KEY", "LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        print("[WARN] Langfuse dataset-project credentials not set. "
              "Set LANGFUSE_DATASET_PUBLIC_KEY + LANGFUSE_DATASET_SECRET_KEY "
              "(or LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY).")

    return Langfuse(public_key=public_key, secret_key=secret_key, host=host)


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
    Format matches what otel_report.py's parse_session() produces.
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
        ev_id = ev.get("id", "")
        if ev_type == "session":
            session_id = ev.get("id", "")
            started_at = ev.get("timestamp", "")
            cwd = ev.get("cwd", "")
        elif ev_type == "message":
            msg = ev.get("message", {})
            if not model and msg.get("model"):
                model = msg.get("model", "")
            if not provider and msg.get("provider"):
                provider = msg.get("provider", "")
            if ev_id:
                messages[ev_id] = ev
                order.append(ev_id)

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
    """Build {toolCallId: event} from all toolResult events."""
    return {
        ev["message"]["toolCallId"]: ev
        for ev in events
        if ev.get("message", {}).get("role") == "toolResult"
        and ev["message"].get("toolCallId")
    }


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
        cmd = tc.get("arguments", {}).get("command", "").lower() if name == "exec" else ""
        return "pact" in name or (bool(cmd) and "caw pact" in cmd)

    def is_tx_call(tc: dict) -> bool:
        name = tc.get("name", "").lower()
        cmd = tc.get("arguments", {}).get("command", "").lower() if name == "exec" else ""
        tx_cmds = ("caw tx transfer", "caw tx call", "caw transfer --to", "caw tx sign")
        return name == "exec" and any(kw in cmd for kw in tx_cmds)

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


def compute_e2e_score(stage_scores: dict[str, dict]) -> dict[str, Any]:
    """
    E2E 端到端综合评分 = 加权平均各阶段 stage_score。
    同时包含 safety 维度（对高风险操作的处理）。
    """
    weighted_sum = 0.0
    for stage, weight in STAGE_WEIGHTS.items():
        score = stage_scores.get(stage, {}).get("stage_score", 5.0)
        weighted_sum += score * weight

    return {
        "stage": "E2E",
        "weighted_composite": round(weighted_sum, 2),
        "stage_breakdown": {
            stage: round(stage_scores.get(stage, {}).get("stage_score", 0.0), 2)
            for stage in STAGE_WEIGHTS
        },
        "stage_score": round(weighted_sum, 2),
    }


# ── LLM-as-Judge ─────────────────────────────────────────────────────────────

def llm_judge(
    full_conversation: str,
    expected: dict,
    metadata: dict,
    api_key: str | None,
) -> dict[str, Any]:
    """
    使用 Claude Haiku 对完整对话进行综合质量评估。
    返回包含各维度分数的 dict；无 API key 时跳过。
    """
    if not api_key:
        return {"stage_score": None, "note": "skipped (no ANTHROPIC_API_KEY)"}

    operation_type = metadata.get("operation_type", "unknown")
    difficulty = metadata.get("difficulty", "L1")
    user_msg = expected.get("user_message", "")
    success_criteria = expected.get("success_criteria", "")

    prompt = f"""你是 CAW (Cobo Agentic Wallet) AI Agent 的专业评估专家。

**测试信息**:
- 操作类型: {operation_type} | 难度: {difficulty}
- 用户指令: {user_msg}
- 成功标准: {success_criteria}

**Agent 完整对话**:
{full_conversation[:3500]}

**评分要求** (各维度 1-10 分，对应 CAW skill 3 阶段):
1. intent_score  (20%): 是否正确理解操作类型、提取 token/amount/chain/address？
2. pact_score    (40%): 是否按规范协商 Pact（4-item preview、最小权限、caw pact submit）？
3. exec_score    (40%): 是否选择正确命令和参数执行交易，正确处理 denial/error，并清晰报告结果？

以 JSON 返回（不要有其他内容）:
{{"intent_score": X, "pact_score": X, "exec_score": X, "weighted_total": X, "comment": "<30字评语>"}}"""

    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            raw = result["content"][0]["text"]
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start >= 0 and end > start:
                scores = json.loads(raw[start:end])
                scores["stage"] = "LLM_JUDGE"
                scores["stage_score"] = scores.get("weighted_total")
                return scores
    except Exception as e:
        return {"stage_score": None, "note": f"LLM error: {e}"}
    return {"stage_score": None, "note": "parse error"}


# ── Main scoring orchestrator ─────────────────────────────────────────────────

def score_trace_full(
    trace_id: str,
    lf: Any,
    item_input: dict,
    item_expected: dict,
    item_metadata: dict,
    anthropic_api_key: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    对单个 trace 进行完整 S1-S3 + E2E 评分。

    流程:
      1. 从 Langfuse 拉取 trace 及其 observations
      2. 提取各阶段相关文本
      3. 运行 S1-S3 阶段评估器
      4. 可选: 运行 LLM-as-Judge 综合评估
      5. 计算 E2E 加权综合分
      6. 通过 Langfuse SDK 创建新的 scoring trace
      7. 通过 Langfuse SDK 将各维度分数上传到原始 trace
    """
    print(f"  → {trace_id[:16]}...")

    # 1. Fetch trace from Langfuse (read via SDK — no backend proxy for trace details)
    try:
        trace = lf.get_trace(trace_id)
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

    # 3-4. Run stage evaluators
    s1 = score_s1_intent_parsing(stage_content["s1"], expected_with_msg, item_metadata)
    s2 = score_s2_pact_negotiation(stage_content["s2"], expected_with_msg, item_metadata)
    s3 = score_s3_execution(stage_content["s3"], expected_with_msg, item_metadata)
    llm = llm_judge(stage_content["full"], expected_with_msg, item_metadata, anthropic_api_key)

    stage_scores = {"s1": s1, "s2": s2, "s3": s3}
    e2e = compute_e2e_score(stage_scores)

    # Optionally blend LLM score into E2E
    if llm.get("stage_score") is not None:
        blended = e2e["stage_score"] * 0.70 + llm["stage_score"] * 0.30
        e2e["llm_blended"] = round(blended, 2)

    result = {
        "trace_id": trace_id,
        "item_metadata": item_metadata,
        "stage_scores": stage_scores,
        "e2e": e2e,
        "llm_judge": llm,
    }

    _print_stage_summary(result)

    if dry_run:
        return result

    # 5. Create scoring trace via Langfuse SDK
    scoring_trace_id = _create_scoring_trace(lf, trace_id, result, item_metadata)
    result["scoring_trace_id"] = scoring_trace_id

    # 6. Upload scores to original trace via Langfuse SDK
    _upload_scores_to_trace(lf, trace_id, stage_scores, e2e, llm)
    lf.flush()

    return result


def score_session_file(
    session_path: str,
    item_input: dict,
    item_expected: dict,
    item_metadata: dict,
    anthropic_api_key: str | None,
    dry_run: bool = False,
    lf: Any = None,
) -> dict[str, Any]:
    """
    对本地 session .jsonl 文件进行完整 S1-S3 + E2E 评分。
    不需要从 Langfuse 读取 trace，直接从本地文件提取各阶段内容。
    session["session_id"] 与 Langfuse trace_id 一致（run_eval.py 上传时已关联）。
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

    s1 = score_s1_intent_parsing(stage_content["s1"], expected_with_msg, item_metadata)
    s2 = score_s2_pact_negotiation(stage_content["s2"], expected_with_msg, item_metadata)
    s3 = score_s3_execution(stage_content["s3"], expected_with_msg, item_metadata)
    llm = llm_judge(stage_content["full"], expected_with_msg, item_metadata, anthropic_api_key)

    stage_scores = {"s1": s1, "s2": s2, "s3": s3}
    e2e = compute_e2e_score(stage_scores)

    if llm.get("stage_score") is not None:
        blended = e2e["stage_score"] * 0.70 + llm["stage_score"] * 0.30
        e2e["llm_blended"] = round(blended, 2)

    result = {
        "trace_id": trace_id,
        "session_path": session_path,
        "item_metadata": item_metadata,
        "stage_scores": stage_scores,
        "e2e": e2e,
        "llm_judge": llm,
    }

    _print_stage_summary(result)

    if dry_run:
        return result

    _lf = lf or _make_langfuse()
    scoring_trace_id = _create_scoring_trace(_lf, trace_id, result, item_metadata)
    result["scoring_trace_id"] = scoring_trace_id
    _upload_scores_to_trace(_lf, trace_id, stage_scores, e2e, llm)
    _lf.flush()

    return result


def _print_stage_summary(result: dict) -> None:
    ss = result["stage_scores"]
    e2e = result["e2e"]
    llm = result.get("llm_judge", {})
    llm_str = f"{llm['stage_score']:.1f}" if llm.get("stage_score") else "N/A"
    print(
        f"    S1={ss['s1']['stage_score']:.1f} "
        f"S2={ss['s2']['stage_score']:.1f} "
        f"S3={ss['s3']['stage_score']:.1f} "
        f"LLM={llm_str} "
        f"→ E2E={e2e['stage_score']:.2f}"
    )


def _create_scoring_trace(
    lf: Any,
    original_trace_id: str,
    result: dict,
    metadata: dict,
) -> str | None:
    """
    通过 Langfuse SDK 在 Langfuse 中创建评分 trace。
    input 指向原始 trace_id，output 包含分阶段评分结果。
    返回新建 trace 的 trace_id。
    """
    try:
        trace = lf.trace(
            name="caw-eval-scoring",
            session_id=original_trace_id,
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
                "llm_judge": result.get("llm_judge", {}),
            },
            metadata={"eval": "true", "evaluated_trace_id": original_trace_id},
        )
        print(f"    [SCORING TRACE] {trace.id}")
        return trace.id
    except Exception as e:
        print(f"    [SCORING TRACE ERROR] {e}")
        return None


def _upload_scores_to_trace(
    lf: Any,
    trace_id: str,
    stage_scores: dict,
    e2e: dict,
    llm: dict,
) -> None:
    """通过 Langfuse SDK 将各阶段分数上传到原始 trace。"""
    scores_to_upload = [
        ("caw.s1_intent", stage_scores["s1"]["stage_score"], "S1 意图解析"),
        ("caw.s2_pact", stage_scores["s2"]["stage_score"], "S2 Pact 协商"),
        ("caw.s3_execution", stage_scores["s3"]["stage_score"], "S3 交易执行"),
        ("caw.e2e_composite", e2e["stage_score"], "E2E 综合 (加权)"),
    ]
    if llm.get("stage_score") is not None:
        scores_to_upload.append(("caw.llm_judge", llm["stage_score"], llm.get("comment", "")))
    if e2e.get("llm_blended") is not None:
        scores_to_upload.append(("caw.e2e_blended", e2e["llm_blended"], "E2E (LLM 混合)"))

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
    anthropic_api_key: str | None,
    dry_run: bool = False,
) -> list[dict]:
    print(f"[INFO] Loading '{dataset_name}' / run '{run_name}' ...")
    try:
        dataset = lf.get_dataset(dataset_name)
        run = lf.get_dataset_run(dataset_name=dataset_name, dataset_run_name=run_name)
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
            anthropic_api_key=anthropic_api_key,
            dry_run=dry_run,
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

    def avg(key: str) -> float:
        vals = [r["stage_scores"][key]["stage_score"] for r in valid if key in r.get("stage_scores", {})]
        return sum(vals) / len(vals) if vals else 0.0

    print(f"\n{'='*70}")
    print(f"Eval Report: {run_name}   ({len(valid)} items)")
    print(f"{'='*70}")
    print(f"  S1 意图解析  (20%): {avg('s1'):5.2f}  |  S3 交易执行  (40%): {avg('s3'):5.2f}")
    print(f"  S2 Pact协商  (40%): {avg('s2'):5.2f}")
    e2e_avg = sum(r["e2e"]["stage_score"] for r in valid) / len(valid)
    print(f"  E2E Composite      : {e2e_avg:5.2f}")
    llm_vals = [r["llm_judge"]["stage_score"] for r in valid if r.get("llm_judge", {}).get("stage_score")]
    if llm_vals:
        print(f"  LLM Judge          : {sum(llm_vals)/len(llm_vals):5.2f}")
    print()

    header = f"{'Item':<14} {'S1':>5} {'S2':>5} {'S3':>5} {'E2E':>6}"
    print(header)
    print("-" * len(header))
    for r in valid:
        ss = r["stage_scores"]
        item_id = r.get("item_id", "?")
        print(
            f"{item_id:<14} "
            f"{ss['s1']['stage_score']:>5.1f} "
            f"{ss['s2']['stage_score']:>5.1f} "
            f"{ss['s3']['stage_score']:>5.1f} "
            f"{r['e2e']['stage_score']:>6.2f}"
        )



def session_main() -> None:
    """
    Subcommand: score one or more local session .jsonl files.

    用法:
        python score_traces.py session --session /path/to/session.jsonl
        python score_traces.py session --session /path/to/sessions_dir/ --report
        python score_traces.py session --session session.jsonl --item-id E2E-01L1 --dataset-name caw-agent-eval-v1
        python score_traces.py session --session session.jsonl --dry-run
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
    args = parser.parse_args(sys.argv[2:])

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    lf = None if args.dry_run else _make_langfuse()

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

    print(f"[INFO] Scoring {len(session_files)} session file(s)...")
    if anthropic_api_key:
        print("[INFO] ANTHROPIC_API_KEY set — LLM-as-Judge enabled")
    else:
        print("[INFO] ANTHROPIC_API_KEY not set — LLM-as-Judge skipped")

    results: list[dict] = []
    for sf in session_files:
        try:
            result = score_session_file(
                str(sf),
                item_input=item_input,
                item_expected=item_expected,
                item_metadata=item_metadata or {"session_file": sf.name},
                anthropic_api_key=anthropic_api_key,
                dry_run=args.dry_run,
                lf=lf,
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
    args = parser.parse_args()

    lf = None if args.dry_run else _make_langfuse()

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        print("[INFO] ANTHROPIC_API_KEY not set — LLM-as-Judge skipped")

    if args.trace_id:
        if lf is None:
            lf = _make_langfuse()
        result = score_trace_full(
            trace_id=args.trace_id,
            lf=lf,
            item_input={},
            item_expected={},
            item_metadata={},
            anthropic_api_key=anthropic_api_key,
            dry_run=args.dry_run,
        )
        results = [result]
    elif args.run_name:
        if lf is None:
            lf = _make_langfuse()
        results = score_dataset_run(
            args.dataset_name, args.run_name, lf, anthropic_api_key, args.dry_run,
        )
    else:
        parser.print_help()
        sys.exit(1)

    if args.report or args.run_name:
        print_report(results, args.run_name or args.trace_id or "")

    if args.output:
        from pathlib import Path
        Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"[SAVED] {args.output}")


if __name__ == "__main__":
    main()
