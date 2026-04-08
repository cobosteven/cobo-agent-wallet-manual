"""
upload_session.py — openclaw session.jsonl → CAW 后端 → Langfuse

用于 caw-eval 评测流程中将 openclaw session 文件上传到 Langfuse results project。
核心逻辑来自 otel_report.py，精简为 caw-eval 专用版本（无 watch 模式）。

上报链路:
  upload_session.py → POST /api/v1/telemetry/session → CAW 后端 → Langfuse results project

用法:
  python upload_session.py session.jsonl
  python upload_session.py ./sessions/          # 批量上传目录下所有 .jsonl
  python upload_session.py session.jsonl --trace-name "eval-run-001"
  python upload_session.py session.jsonl --dry-run  # 仅解析，不上传

环境变量:
  AGENT_WALLET_API_URL  CAW 后端 URL（必填）
  CAW_API_KEY           CAW API Key（必填）
"""

import getpass
import glob
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ── caw 操作分类表 ─────────────────────────────────────────────────────────────

CAW_OP_TABLE = [
    (["onboard bootstrap"],           "caw.onboard.bootstrap", "onboarding"),
    (["onboard health"],              "caw.onboard.health",    "onboarding"),
    (["onboard self-test"],           "caw.onboard.self_test", "onboarding"),
    (["onboard"],                     "caw.onboard",           "onboarding"),
    (["tx transfer"],                 "caw.tx.transfer",       "transaction"),
    (["tx call"],                     "caw.tx.call",           "transaction"),
    (["tx sign-message"],             "caw.tx.sign_message",   "transaction"),
    (["tx speedup"],                  "caw.tx.speedup",        "transaction"),
    (["tx drop"],                     "caw.tx.drop",           "transaction"),
    (["tx estimate-transfer-fee"],    "caw.tx.estimate_fee",   "query"),
    (["tx estimate-call-fee"],        "caw.tx.estimate_call_fee", "query"),
    (["tx list"],                     "caw.tx.list",           "query"),
    (["tx get"],                      "caw.tx.get",            "query"),
    (["wallet balance"],              "caw.wallet.balance",    "query"),
    (["wallet list"],                 "caw.wallet.list",       "query"),
    (["wallet get"],                  "caw.wallet.get",        "query"),
    (["wallet current"],              "caw.wallet.current",    "query"),
    (["wallet pair-status"],          "caw.wallet.pair_status","wallet"),
    (["wallet pair"],                 "caw.wallet.pair",       "wallet"),
    (["wallet rename"],               "caw.wallet.rename",     "wallet"),
    (["wallet archive"],              "caw.wallet.archive",    "wallet"),
    (["wallet update"],               "caw.wallet.update",     "wallet"),
    (["address create"],              "caw.address.create",    "wallet"),
    (["address list"],                "caw.address.list",      "query"),
    (["status"],                      "caw.status",            "query"),
    (["pending approve"],             "caw.pending.approve",   "auth"),
    (["pending reject"],              "caw.pending.reject",    "auth"),
    (["pending list"],                "caw.pending.list",      "auth"),
    (["pending get"],                 "caw.pending.get",       "auth"),
    (["pact submit"],                 "caw.pact.submit",       "auth"),
    (["pact status"],                 "caw.pact.status",       "auth"),
    (["pact show"],                   "caw.pact.show",         "auth"),
    (["pact events"],                 "caw.pact.events",       "auth"),
    (["pact list"],                   "caw.pact.list",         "auth"),
    (["pact revoke"],                 "caw.pact.revoke",       "auth"),
    (["pact withdraw"],               "caw.pact.withdraw",     "auth"),
    (["pact update-conditions"],      "caw.pact.update_conditions", "auth"),
    (["pact update-policies"],        "caw.pact.update_policies",  "auth"),
    (["approval create"],             "caw.approval.create",   "auth"),
    (["approval resolve"],            "caw.approval.resolve",  "auth"),
    (["approval list"],               "caw.approval.list",     "auth"),
    (["approval get"],                "caw.approval.get",      "auth"),
    (["track"],                       "caw.track",             "monitor"),
    (["node status"],                 "caw.node.status",       "node"),
    (["node start"],                  "caw.node.start",        "node"),
    (["node stop"],                   "caw.node.stop",         "node"),
    (["node restart"],                "caw.node.restart",      "node"),
    (["node health"],                 "caw.node.health",       "node"),
    (["node info"],                   "caw.node.info",         "node"),
    (["node logs"],                   "caw.node.logs",         "node"),
    (["meta chain-info"],             "caw.meta.chain_info",   "meta"),
    (["meta search-tokens"],          "caw.meta.search_tokens","meta"),
    (["meta prices"],                 "caw.meta.prices",       "meta"),
    (["meta chains"],                 "caw.meta.chains",       "meta"),
    (["meta tokens"],                 "caw.meta.tokens",       "meta"),
    (["faucet deposit"],              "caw.faucet.deposit",    "dev"),
    (["faucet tokens"],               "caw.faucet.tokens",     "dev"),
    (["update"],                      "caw.update",            "meta"),
    (["fetch"],                       "caw.fetch",             "util"),
    (["export-key"],                  "caw.export_key",        "wallet"),
    (["demo"],                        "caw.demo",              "dev"),
    (["schema"],                      "caw.schema",            "meta"),
    (["version", "--version"],        "caw.version",           "meta"),
    (["--help", "-h"],                "caw.help",              "meta"),
]

CAW_BIN_PATTERN = re.compile(
    r"(?:^|&&\s*)"
    r"(?:[^\s]*?/)?caw\s+"
    r"(.*?)(?:\s+&&|\s*$)",
    re.MULTILINE
)
SKILL_INSTALL_PATTERN = re.compile(
    r"(?:npx\s+skills\s+add|clawhub\s+install|npx\s+skills\s+update)\s+(\S+)"
)
BOOTSTRAP_PATTERN = re.compile(r"bootstrap-env\.sh")
POLICY_DENIAL_PATTERN = re.compile(
    r"(?:TRANSFER_LIMIT_EXCEEDED|POLICY_DENIED|403|policy.*denied|suggestion[\":\s]+([^\n]+))",
    re.IGNORECASE
)
UPDATE_SIGNAL = re.compile(r'"update"\s*:\s*true')


# ── 配置读取 ──────────────────────────────────────────────────────────────────

def load_caw_config() -> dict[str, str]:
    """从 ~/.cobo-agentic-wallet/ 读取 API key/URL 等，env vars 优先覆盖。"""
    result: dict[str, str] = {}
    config_path = Path.home() / ".cobo-agentic-wallet" / "config"
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
        profile_id = cfg.get("default_profile", "")
        if profile_id:
            cred_path = (Path.home() / ".cobo-agentic-wallet" / "profiles"
                         / f"profile_{profile_id}" / "credentials")
            if cred_path.exists():
                cred = json.loads(cred_path.read_text())
                result["api_key"] = cred.get("api_key", "")
                result["api_url"] = cred.get("api_url", "")
                result["agent_id"] = cred.get("agent_id", "")
                result["wallet_uuid"] = cred.get("wallet_uuid", "")
                result["env"] = cred.get("env", "")

    if v := os.environ.get("CAW_API_KEY"):
        result["api_key"] = v
    if v := os.environ.get("AGENT_WALLET_API_URL"):
        result["api_url"] = v
    return result


# ── JSONL 解析 ────────────────────────────────────────────────────────────────

def parse_session(path: str) -> dict:
    messages: dict = {}
    order: list = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            eid = ev.get("id") or ev.get("type", "")
            if eid:
                messages[eid] = ev
                order.append(eid)

    session_ev = next((messages[i] for i in order if messages[i].get("type") == "session"), {})
    snapshot = next(
        (messages[i]["data"] for i in order if messages[i].get("customType") == "model-snapshot"),
        {}
    )
    return {
        "session_id": session_ev.get("id", Path(path).stem),
        "started_at": session_ev.get("timestamp"),
        "cwd": session_ev.get("cwd", ""),
        "model": snapshot.get("modelId", "unknown"),
        "provider": snapshot.get("provider", "unknown"),
        "messages": messages,
        "order": order,
    }


def extract_message_events(session: dict) -> list[dict]:
    return [session["messages"][i] for i in session["order"]
            if session["messages"][i].get("type") == "message"]


def build_turns(message_events: list[dict]) -> list[list[dict]]:
    turns: list = []
    current: list = []
    for ev in message_events:
        role = ev.get("message", {}).get("role")
        if role == "user" and current:
            turns.append(current)
            current = []
        current.append(ev)
    if current:
        turns.append(current)
    return turns


def build_tool_result_index(message_events: list[dict]) -> dict:
    return {
        ev["message"]["toolCallId"]: ev
        for ev in message_events
        if ev.get("message", {}).get("role") == "toolResult"
        and ev["message"].get("toolCallId")
    }


# ── caw 命令解析 ──────────────────────────────────────────────────────────────

def parse_caw_command(command: str) -> Optional[tuple[str, str, str]]:
    m = CAW_BIN_PATTERN.search(command)
    if not m:
        return None
    subcmd = m.group(1).strip()
    if "--help" in subcmd or subcmd.endswith("-h"):
        return "caw.help", "meta", subcmd
    clean = re.sub(r"--(?:format|env|profile|timeout|verbose|api-key|api-url)\s*\S*", "", subcmd).strip()
    for prefixes, span_name, category in CAW_OP_TABLE:
        for p in prefixes:
            if clean.startswith(p):
                return span_name, category, subcmd
    return "caw.unknown", "unknown", subcmd


def extract_caw_flags(subcmd: str) -> dict:
    flags = {}
    for flag, key in [
        (r"--to\s+(\S+)",          "to_address"),
        (r"--token-id\s+(\S+)",    "token_id"),
        (r"--amount\s+(\S+)",      "amount"),
        (r"--chain\s+(\S+)",       "chain"),
        (r"--request-id\s+(\S+)",  "request_id"),
        (r"--wallet-id\s+(\S+)",   "wallet_id"),
        (r"--env\s+(\S+)",         "env"),
        (r"--contract\s+(\S+)",    "contract"),
        (r"--context\s+'([^']+)'", "context"),
    ]:
        hit = re.search(flag, subcmd)
        if hit:
            flags[key] = hit.group(1)
    return flags


def parse_tx_result(text: str) -> dict:
    result: dict = {}
    try:
        data = json.loads(text)
        inner = data.get("result", data)
        for k in ["transaction_id", "tx_hash", "status", "request_id", "error_code", "suggestion"]:
            if k in inner:
                result[k] = str(inner[k])
        if data.get("update"):
            result["caw_update_available"] = "true"
    except Exception:
        m = POLICY_DENIAL_PATTERN.search(text)
        if m:
            result["policy_denial"] = m.group(0)[:200]
        if UPDATE_SIGNAL.search(text):
            result["caw_update_available"] = "true"
    return result


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def ts_to_ns(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)
    except Exception:
        return None


def safe_str(obj: object, limit: int = 2000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
        return s[:limit]
    except Exception:
        return str(obj)[:limit]


def extract_user_text(msg: dict) -> str:
    parts = []
    for block in msg.get("content", []):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        text = re.sub(
            r"Conversation info \(untrusted metadata\):.*?(?=\n\n|\Z)",
            "", text, flags=re.DOTALL
        ).strip()
        text = re.sub(r"^System:.*", "", text, flags=re.MULTILINE).strip()
        text = re.sub(
            r"Sender \(untrusted metadata\):\s*```json\s*\{.*?\}\s*```",
            "", text, flags=re.DOTALL
        ).strip()
        if text:
            parts.append(text[:400])
    return " | ".join(parts)


def extract_sender_id(msg: dict) -> str:
    for block in msg.get("content", []):
        text = block.get("text", "")
        m = re.search(r'"sender_id":\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        m = re.search(r'"id":\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return ""


def extract_sender_name(msg: dict) -> str:
    for block in msg.get("content", []):
        m = re.search(r'"sender":\s*"([^"]+)"', block.get("text", ""))
        if m:
            return m.group(1)
    return "unknown"


# ── HTTP 上报 ─────────────────────────────────────────────────────────────────

def post_session(api_url: str, api_key: str, record: dict) -> bool:
    """POST session record 到 /api/v1/telemetry/session。"""
    url = f"{api_url.rstrip('/')}/api/v1/telemetry/session"
    data = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"[WARN] POST {url} → {e.code}: {e.read()[:500].decode(errors='replace')}")
        return False
    except Exception as e:
        print(f"[WARN] POST {url} → {e}")
        return False


# ── SessionUploader ───────────────────────────────────────────────────────────

class SessionUploader:
    """解析 session.jsonl，构造 SessionRecord JSON，POST 上报。"""

    def __init__(self, api_url: str, api_key: str,
                 skill_name: str = "cobo-agentic-wallet-sandbox",
                 resource: Optional[dict[str, str]] = None,
                 trace_name: str = ""):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.skill = skill_name
        self.resource = resource or {}
        self.trace_name = trace_name

    def upload(self, session: dict, user_id: str = "") -> bool:
        evts = extract_message_events(session)
        turns = build_turns(evts)
        tr_idx = build_tool_result_index(evts)

        sid = session["session_id"]
        model = session["model"]
        prov = session["provider"]

        first_user = next(
            (e for e in evts if e.get("message", {}).get("role") == "user"), None
        )
        if first_user and not user_id:
            user_id = extract_sender_id(first_user.get("message", {})) or "unknown"

        start_ns = ts_to_ns(session["started_at"])
        all_events = [ev for turn in turns for ev in turn]
        last_ns = ts_to_ns(all_events[-1].get("timestamp")) if all_events else start_ns

        tz_cn = timezone(offset=timedelta(hours=8))
        now_cn = datetime.now(tz=tz_cn)
        time_code = now_cn.strftime("%m%d%H%M")
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        hostname = socket.gethostname()
        trace_display_name = self.trace_name or f"eval_{user}@{hostname}_{time_code}"
        upload_iso = now_cn.isoformat()

        turn_children = [
            self._build_turn_record(turn, i, model, prov, tr_idx)
            for i, turn in enumerate(turns)
        ]

        session_record: dict = {
            "name": f"session:{sid[:8]}",
            "trace_name": trace_display_name,
            "session_id": sid,
            "user_id": user_id,
            "tags": ["openclaw", "caw-eval"],
            "start_time_unix_nano": start_ns,
            "end_time_unix_nano": last_ns,
            "metadata": {
                "skill": self.skill,
                "model": model,
                "provider": prov,
                "cwd": session.get("cwd", ""),
                "session_id": sid,
                "telemetry_source": "caw-eval",
                "uploaded_at": upload_iso,
                "host": f"{getpass.getuser()}@{socket.gethostname()}",
            },
            "attributes": {
                "langfuse.observation.input": safe_str({
                    "session_id": sid,
                    "model": model,
                    "turns": len(turns),
                }),
            },
            "children": turn_children,
        }

        ok = post_session(self.api_url, self.api_key, session_record)
        total_children = sum(len(t.get("children") or []) for t in turn_children)
        status = "OK" if ok else "FAILED"
        print(f"\n{'='*60}")
        print(f"  Status:      {status}")
        print(f"  Trace Name:  {trace_display_name}")
        print(f"  Session ID:  {sid}")
        print(f"  User ID:     {user_id}")
        print(f"  Model:       {model}")
        print(f"  Turns:       {len(turn_children)}")
        print(f"  Spans:       {total_children}")
        print(f"  API:         {self.api_url}")
        print(f"{'='*60}")
        return ok

    def _build_turn_record(self, turn: list, idx: int, model: str, provider: str,
                            tr_idx: dict) -> dict:
        user_ev = turn[0]
        user_msg = user_ev.get("message", {})
        user_text_raw = extract_user_text(user_msg)
        sender = extract_sender_name(user_msg)
        turn_start_ns = ts_to_ns(user_ev.get("timestamp"))
        turn_end_ns = ts_to_ns(turn[-1].get("timestamp")) if turn else turn_start_ns

        events_after_user = turn[1:]
        children: list = []
        final_text = ""
        for j, ev in enumerate(events_after_user):
            msg = ev.get("message", {})
            role = msg.get("role")
            if role == "assistant":
                next_ts = None
                if j + 1 < len(events_after_user):
                    next_ts = ts_to_ns(events_after_user[j + 1].get("timestamp"))
                llm_children = self._build_assistant_children(ev, model, provider, tr_idx, next_ts)
                children.extend(llm_children)
                for b in msg.get("content", []):
                    if b.get("type") == "text":
                        final_text = b.get("text", "")[:500]

        input_preview = user_text_raw[:10].rstrip() + ".." if len(user_text_raw) > 10 else user_text_raw
        turn_name = f'turn:{idx} ("{input_preview}")' if input_preview else f"turn:{idx}"

        return {
            "name": turn_name,
            "record_type": "span",
            "start_time_unix_nano": turn_start_ns,
            "end_time_unix_nano": turn_end_ns,
            "attributes": {
                "langfuse.observation.input": safe_str({"role": "user", "content": user_text_raw}),
                "langfuse.observation.output": (
                    safe_str({"role": "assistant", "content": final_text}) if final_text else None
                ),
                "langfuse.trace.metadata.turn_index": str(idx),
                "langfuse.trace.metadata.sender": sender,
            },
            "children": children if children else None,
        }

    def _build_assistant_children(self, ev: dict, model: str, provider: str,
                                   tr_idx: dict, next_ev_ts: Optional[int] = None) -> list:
        children: list = []
        msg = ev.get("message", {})
        content = msg.get("content", [])
        usage = msg.get("usage", {})
        ts_ns = ts_to_ns(ev.get("timestamp"))

        tool_calls = [b for b in content if b.get("type") == "toolCall"]

        msg_ts = msg.get("timestamp")
        if msg_ts and ts_ns:
            llm_start = int(msg_ts * 1e6) if isinstance(msg_ts, (int, float)) else ts_ns
            llm_end = ts_ns
        else:
            llm_start = ts_ns
            llm_end = next_ev_ts or ts_ns

        children.append({
            "name": "OpenAI-generation",
            "record_type": "generation",
            "status_code": "OK",
            "start_time_unix_nano": llm_start,
            "end_time_unix_nano": llm_end,
            "attributes": {
                "gen_ai.request.model": msg.get("model", model),
                "langfuse.observation.model.name": msg.get("model", model),
                "gen_ai.usage.input_tokens": usage.get("input", 0),
                "gen_ai.usage.output_tokens": usage.get("output", 0),
                "langfuse.observation.output": safe_str(
                    [b.get("name") or b.get("text", "")[:80] for b in content[:5]]
                ),
                "langfuse.trace.metadata.provider": provider,
                "langfuse.trace.metadata.api": msg.get("api", ""),
                "langfuse.trace.metadata.stop_reason": msg.get("stopReason", ""),
                "langfuse.trace.metadata.response_id": msg.get("responseId", ""),
                "langfuse.observation.metadata.tool_calls_count": str(len(tool_calls)),
            },
        })

        for tc in tool_calls:
            child = self._build_tool_child(tc, tr_idx, ts_ns)
            if child:
                children.append(child)

        return children

    def _build_tool_child(self, tc: dict, tr_idx: dict,
                           fallback_ts_ns: Optional[int]) -> Optional[dict]:
        call_id = tc.get("id", "")
        name = tc.get("name", "")
        args = tc.get("arguments", {})

        result_ev = tr_idx.get(call_id)
        result_msg = result_ev.get("message", {}) if result_ev else {}
        details = result_msg.get("details", {})
        result_ts_ns = ts_to_ns(result_ev.get("timestamp")) if result_ev else fallback_ts_ns
        dur_ms = details.get("durationMs", 0)
        if not dur_ms and fallback_ts_ns and result_ts_ns and result_ts_ns > fallback_ts_ns:
            dur_ms = int((result_ts_ns - fallback_ts_ns) / 1e6)
        ts_ns = fallback_ts_ns or result_ts_ns
        exit_code = details.get("exitCode")
        status_ok = exit_code is None or exit_code == 0

        result_text = ""
        for b in result_msg.get("content", []):
            if b.get("type") == "text":
                result_text = b.get("text", "")
                break

        if name == "exec":
            cmd = args.get("command", "")
            caw_info = parse_caw_command(cmd)
            if caw_info:
                span_name, category, subcmd = caw_info
                return self._build_caw_child(
                    span_name, category, subcmd, result_text,
                    dur_ms, ts_ns, result_ts_ns, status_ok, exit_code
                )
            if SKILL_INSTALL_PATTERN.search(cmd):
                category = "skill_install"
            elif BOOTSTRAP_PATTERN.search(cmd):
                category = "env_bootstrap"
            else:
                category = "exec"
        elif name == "read":
            category = "file_read"
        elif name == "web_search":
            category = "web_search"
        elif name == "process":
            category = "process_poll"
        else:
            category = name

        attrs: dict = {
            "langfuse.observation.input": safe_str(args, 800),
            "langfuse.observation.output": result_text[:800],
            "langfuse.observation.metadata.tool_call_id": call_id,
            "langfuse.observation.metadata.tool_name": name,
            "langfuse.observation.metadata.category": category,
            "langfuse.observation.metadata.duration_ms": str(dur_ms),
            "langfuse.observation.metadata.exit_code": str(exit_code),
        }
        if category == "skill_install":
            m = SKILL_INSTALL_PATTERN.search(args.get("command", ""))
            if m:
                attrs["langfuse.trace.metadata.skill_package"] = m.group(1)

        end_ns = result_ts_ns or (ts_ns + int(dur_ms * 1e6) if ts_ns and dur_ms else ts_ns)
        return {
            "name": f"{category}:{name}",
            "record_type": "span",
            "start_time_unix_nano": ts_ns,
            "end_time_unix_nano": end_ns,
            "status_code": "OK" if status_ok else "ERROR",
            "status_message": "" if status_ok else result_text[:200],
            "attributes": attrs,
        }

    def _build_caw_child(self, span_name: str, category: str, subcmd: str,
                          result_text: str, dur_ms: int, ts_ns: Optional[int],
                          result_ts_ns: Optional[int], status_ok: bool,
                          exit_code: Optional[int]) -> dict:
        flags = extract_caw_flags(subcmd)

        attrs: dict = {
            "langfuse.observation.input": safe_str({"subcmd": subcmd[:300]}),
            "langfuse.observation.output": result_text[:1000],
            "langfuse.observation.metadata.caw_op": span_name,
            "langfuse.observation.metadata.category": category,
            "langfuse.observation.metadata.duration_ms": str(dur_ms),
            "langfuse.observation.metadata.exit_code": str(exit_code),
            "langfuse.trace.metadata.caw_op": span_name,
            "langfuse.trace.metadata.caw_category": category,
        }
        for k, v in flags.items():
            attrs[f"langfuse.trace.metadata.caw_{k}"] = v

        if category == "transaction":
            tx_fields = parse_tx_result(result_text)
            for k, v in tx_fields.items():
                attrs[f"langfuse.trace.metadata.tx_{k}"] = v
            if "policy_denial" in tx_fields or not status_ok:
                attrs["langfuse.observation.level"] = "WARNING"
                attrs["langfuse.observation.metadata.policy_denied"] = "true"

        if UPDATE_SIGNAL.search(result_text):
            attrs["langfuse.trace.metadata.caw_update_available"] = "true"

        if "context" in flags:
            try:
                ctx = json.loads(flags["context"])
                attrs["langfuse.trace.metadata.openclaw_channel"] = ctx.get("channel", "")
                attrs["langfuse.trace.metadata.openclaw_target"] = ctx.get("target", "")
            except Exception:
                pass

        status = "OK"
        if not status_ok and category not in ("query", "meta", "dev"):
            status = "ERROR"

        end_ns = result_ts_ns or (ts_ns + int(dur_ms * 1e6) if ts_ns and dur_ms else ts_ns)
        return {
            "name": span_name,
            "record_type": "span",
            "start_time_unix_nano": ts_ns,
            "end_time_unix_nano": end_ns,
            "status_code": status,
            "status_message": "" if status == "OK" else result_text[:200],
            "attributes": attrs,
        }


# ── 公开 API ──────────────────────────────────────────────────────────────────

def extract_session_id(jsonl_path: str) -> str:
    """从 JSONL 文件提取 session_id。"""
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("type") == "session":
                    return ev.get("id", Path(jsonl_path).stem)
    except Exception:
        pass
    return Path(jsonl_path).stem


def upload_session_file(
    jsonl_path: str,
    api_url: str = "",
    api_key: str = "",
    user_id: str = "",
    skill_name: str = "cobo-agentic-wallet-sandbox",
    trace_name: str = "",
) -> bool:
    """上传单个 session.jsonl 到 CAW 后端。返回是否成功。"""
    caw_cfg = load_caw_config()
    api_url = api_url or caw_cfg.get("api_url", "") or "https://api-core.agenticwallet.sandbox.cobo.com"
    api_key = api_key or caw_cfg.get("api_key", "") or ""

    if not api_url:
        print("[ERROR] 缺少 api_url。请设置 AGENT_WALLET_API_URL 环境变量。", file=sys.stderr)
        return False

    resource = {
        "caw.agent_id": caw_cfg.get("agent_id", ""),
        "caw.wallet_id": caw_cfg.get("wallet_uuid", ""),
        "deployment.environment": caw_cfg.get("env", ""),
        "server.address": api_url,
    }

    session = parse_session(jsonl_path)
    evts = extract_message_events(session)
    print(f"[INFO] Parsed {session['session_id']}  model={session['model']}  "
          f"events={len(evts)}")

    uploader = SessionUploader(api_url, api_key, skill_name, resource, trace_name=trace_name)
    return uploader.upload(session, user_id=user_id)


# ── dry-run 打印 span 树 ───────────────────────────────────────────────────────

def dry_run_session(jsonl_path: str) -> None:
    session = parse_session(jsonl_path)
    evts = extract_message_events(session)
    turns = build_turns(evts)
    print(f"{'='*60}")
    print(f"Session: {session['session_id']}")
    print(f"Model:   {session['model']}")
    print(f"Started: {session['started_at']}")
    print(f"Turns:   {len(turns)}")
    print(f"Events:  {len(evts)}")
    print(f"{'='*60}")
    for i, turn in enumerate(turns):
        user_ev = turn[0]
        user_text = extract_user_text(user_ev.get("message", {}))
        ts = user_ev.get("timestamp", "?")
        print(f"[turn:{i}]  [{ts}]  user: {user_text[:80]}")
        for ev in turn[1:]:
            msg = ev.get("message", {})
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                tool_calls = [b for b in content if b.get("type") == "toolCall"]
                usage = msg.get("usage", {})
                print(f"  +- generation  tokens={usage.get('input',0)}+{usage.get('output',0)}"
                      f"  tools={len(tool_calls)}")
        print()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="upload_session.py",
        description="Upload openclaw session.jsonl to Langfuse via CAW backend",
    )
    parser.add_argument("paths", nargs="+",
                        help="Session .jsonl file(s) or directory containing .jsonl files")
    parser.add_argument("--api-url", default="",
                        help="CAW backend URL (or AGENT_WALLET_API_URL env)")
    parser.add_argument("--api-key", default="",
                        help="CAW API key (or CAW_API_KEY env)")
    parser.add_argument("--skill", default="cobo-agentic-wallet-sandbox",
                        help="Skill name tag (default: cobo-agentic-wallet-sandbox)")
    parser.add_argument("--trace-name", default="",
                        help="Override Langfuse trace display name")
    parser.add_argument("--user-id", default="",
                        help="Override user ID in trace metadata")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print span tree without uploading")
    args = parser.parse_args()

    # Collect all .jsonl files from paths
    jsonl_files: list[str] = []
    for p in args.paths:
        if os.path.isdir(p):
            jsonl_files.extend(sorted(f for f in glob.glob(os.path.join(p, "*.jsonl"))
                                       if not f.endswith(".lock")))
        elif p.endswith(".jsonl") and os.path.isfile(p):
            jsonl_files.append(p)
        else:
            expanded = glob.glob(p)
            jsonl_files.extend(f for f in expanded
                                if f.endswith(".jsonl") and not f.endswith(".lock"))

    if not jsonl_files:
        print("[ERROR] No .jsonl files found", file=sys.stderr)
        sys.exit(1)

    api_url = args.api_url or os.environ.get("AGENT_WALLET_API_URL", "")
    api_key = args.api_key or os.environ.get("CAW_API_KEY", "")

    failed = 0
    for idx, path in enumerate(jsonl_files):
        if len(jsonl_files) > 1:
            print(f"\n[{idx + 1}/{len(jsonl_files)}] {os.path.basename(path)}")
        if args.dry_run:
            dry_run_session(path)
        else:
            ok = upload_session_file(
                path,
                api_url=api_url,
                api_key=api_key,
                user_id=args.user_id,
                skill_name=args.skill,
                trace_name=args.trace_name,
            )
            if not ok:
                failed += 1

    if not args.dry_run and failed:
        print(f"\n[ERROR] {failed}/{len(jsonl_files)} uploads failed", file=sys.stderr)
        sys.exit(1)
