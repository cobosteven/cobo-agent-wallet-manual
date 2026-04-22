#!/usr/bin/env bash
# sync_to_servers.sh — 组件级条件同步 + MD5/hash 校验
#
# 用途：
#   把本地 sandbox skill / caw-eval 脚本 / caw CLI 二进制 scp 到评测服务器。
#   - 用 git tree hash 对比本地 vs 服务器，只推 hash 不同的组件（节省带宽）
#   - 推后独立 verify：ssh 实际重算 hash 对比
#   - 失败自动重试 1 次，再失败 abort
#
# 用法：
#   sync_to_servers.sh --component <scripts|skill|caw-cli|recipes|all> --verify
#   sync_to_servers.sh --component all --verify [--servers-env SERVERS_GPT]
#
# 要求：
#   本地：git、gcloud auth login、Go 工具链（caw-cli 需要）
#   服务器：已装 caw + ~/.agents/skills/ 结构

set -euo pipefail

# ── 默认配置 ─────────────────────────────────────────────────────────────────
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../../" && pwd)}"
SDK_DIR="$REPO_ROOT/cobo-agent-wallet/sdk"
SKILL_LOCAL="$SDK_DIR/skills/cobo-agentic-wallet-sandbox"
SCRIPTS_LOCAL="$SDK_DIR/skills/caw-eval/scripts"
CAW_BUILD="$SDK_DIR/go/build/bin/caw"
RECIPES_LOCAL="${RECIPES_LOCAL:-/tmp/caw-eval-recipes}"

# 远端路径
SKILL_REMOTE="~/.agents/skills/cobo-agentic-wallet-sandbox"
SCRIPTS_REMOTE="~/.agents/skills/caw-eval/scripts"
CAW_REMOTE="~/.cobo-agentic-wallet/bin/caw"
RECIPES_REMOTE="/tmp/caw-eval-recipes"

# 结构化日志
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# ── 参数解析 ─────────────────────────────────────────────────────────────────
COMPONENT="all"
VERIFY=0
SERVERS_ENV="${SERVERS_ENV:-SERVERS_GPT}"  # 默认用 SERVERS_GPT 变量名，可 env 覆盖

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="$2"; shift 2 ;;
    --verify) VERIFY=1; shift ;;
    --servers-env) SERVERS_ENV="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) log "unknown arg: $1"; exit 2 ;;
  esac
done

# ── 服务器列表（从 env var 读入）────────────────────────────────────────────────
# 格式：name:zone:project，空格分隔，如：
#   SERVERS_GPT="server1:zone1:proj1 server2:zone2:proj2"
SERVERS_RAW="${!SERVERS_ENV:-}"
if [[ -z "$SERVERS_RAW" ]]; then
  log "ERROR: env var $SERVERS_ENV 未设置，格式：'name:zone:project name:zone:project'"
  exit 2
fi
read -ra SERVERS <<< "$SERVERS_RAW"

# ── Git 工作区检查 ────────────────────────────────────────────────────────────
log "[preflight] 检查 git 工作区"
cd "$REPO_ROOT"
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  log "ERROR: $REPO_ROOT 不是 git 仓库"
  exit 2
fi
if [[ -n "$(git status --porcelain cobo-agent-wallet/sdk/skills/caw-eval cobo-agent-wallet/sdk/skills/cobo-agentic-wallet-sandbox cobo-agent-wallet/sdk/go 2>/dev/null)" ]]; then
  log "WARN: 相关目录有未 commit 改动，precheck 的 git hash 不代表服务器上的实际内容"
  log "      建议先 git add && commit，再同步"
fi

# ── Hash 计算（本地） ──────────────────────────────────────────────────────────
# 重要：本地和远端算法必须对称（find + sort + shasum），git tree hash 和 find+shasum
# 永远不会相等，bsdtar 和 gnu tar 参数不兼容 tar stream 也算不出相同 hash。
# 用 cut -c 1-64 取 hex hash 前 64 字符（避开 awk '{print $1}' 嵌套引号问题）。
local_hash_skill() {
  if [[ -d "$SKILL_LOCAL" ]]; then
    (cd "$SKILL_LOCAL" && find . -type f ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64)
  else
    echo "missing"
  fi
}
local_hash_scripts() {
  if [[ -d "$SCRIPTS_LOCAL" ]]; then
    (cd "$SCRIPTS_LOCAL" && find . -type f ! -name '*.pyc' ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64)
  else
    echo "missing"
  fi
}
local_hash_caw() {
  if [[ -f "$CAW_BUILD" ]]; then
    shasum -a 256 "$CAW_BUILD" | cut -c 1-64
  else
    echo "missing"
  fi
}
local_hash_recipes() {
  if [[ -d "$RECIPES_LOCAL" ]]; then
    (cd "$RECIPES_LOCAL" && find . -type f ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64)
  else
    echo "missing"
  fi
}

# ── Hash 计算（远端） ──────────────────────────────────────────────────────────
# 远端服务器读一份 manifest；若没有则返回 "absent"。
# 服务器端 manifest 格式：每行 `component=hash`
remote_hash() {
  local srv_spec="$1" component="$2"
  IFS=':' read -r name zone project <<< "$srv_spec"
  # 用 cut -c 1-64 取 shasum 输出的前 64 字节（= hex hash）
  # 避开 awk '{print $1}' 在多层嵌套引号里 $1 被远端 shell 展开成空的坑
  local cmd
  case "$component" in
    skill)
      # 和本地 local_hash_skill 算法对称：cd + find . 输出相对路径，xargs shasum 行才一致
      cmd="test -d $SKILL_REMOTE && (cd $SKILL_REMOTE && find . -type f ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64) || echo absent"
      ;;
    scripts)
      cmd="test -d $SCRIPTS_REMOTE && (cd $SCRIPTS_REMOTE && find . -type f ! -name '*.pyc' ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64) || echo absent"
      ;;
    caw-cli)
      cmd="test -f $CAW_REMOTE && shasum -a 256 $CAW_REMOTE | cut -c 1-64 || echo absent"
      ;;
    recipes)
      cmd="test -d $RECIPES_REMOTE && (cd $RECIPES_REMOTE && find . -type f ! -name '.DS_Store' -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -c 1-64) || echo absent"
      ;;
    *) echo "absent"; return ;;
  esac
  local raw
  raw=$(gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
    -- "sudo su - ubuntu -c \"$cmd\"" 2>/dev/null | tail -1)
  # trim 所有空白（SSH 输出可能带 \r / 尾部空格）
  echo "${raw//[[:space:]]/}"
}

# ── 推送单个组件到单台服务器 ─────────────────────────────────────────────────
push_skill() {
  local srv_spec="$1"
  IFS=':' read -r name zone project <<< "$srv_spec"
  tar czf - -C "$(dirname "$SKILL_LOCAL")" \
      --exclude='.DS_Store' --exclude='__pycache__' \
      "$(basename "$SKILL_LOCAL")" \
    | gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
        -- "sudo su - ubuntu -c 'mkdir -p ~/.agents/skills && cd ~/.agents/skills && tar xzf -'" 2>&1
}

push_scripts() {
  local srv_spec="$1"
  IFS=':' read -r name zone project <<< "$srv_spec"
  tar czf - -C "$(dirname "$SCRIPTS_LOCAL")" \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
      "$(basename "$SCRIPTS_LOCAL")" \
    | gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
        -- "sudo su - ubuntu -c 'mkdir -p ~/.agents/skills/caw-eval && cd ~/.agents/skills/caw-eval && tar xzf -'" 2>&1
}

build_caw_if_needed() {
  if [[ ! -f "$CAW_BUILD" ]] || [[ "${CAW_REBUILD:-0}" == "1" ]]; then
    log "[caw-cli] 交叉编译 linux/amd64"
    (cd "$SDK_DIR" && GOOS=linux GOARCH=amd64 make build-caw) >&2
  fi
}

push_caw_cli() {
  local srv_spec="$1"
  IFS=':' read -r name zone project <<< "$srv_spec"
  build_caw_if_needed
  if [[ ! -f "$CAW_BUILD" ]]; then
    log "[$name] [caw-cli] build 失败，无 $CAW_BUILD"
    return 1
  fi
  # /tmp/caw-new 在 scp 时以 luochong_cobo_com 身份写入，/tmp sticky bit + owner 冲突使得
  # ubuntu 用户无法 rm。用唯一临时名避免碰撞，install 后用 sudo rm（失败不致命，tmpfiles 会清理）
  local nonce
  nonce="caw-new-$$-$RANDOM"
  local tmp_remote="/tmp/$nonce"
  gcloud compute scp --zone "$zone" --tunnel-through-iap --project "$project" \
    "$CAW_BUILD" "$name:$tmp_remote" 2>&1 >&2 && \
  gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
    -- "sudo su - ubuntu -c 'install -m 0755 $tmp_remote $CAW_REMOTE; sudo rm -f $tmp_remote 2>/dev/null || true'" 2>&1
}

push_recipes() {
  local srv_spec="$1"
  IFS=':' read -r name zone project <<< "$srv_spec"
  if [[ ! -d "$RECIPES_LOCAL" ]]; then
    log "[$name] [recipes] 本地 $RECIPES_LOCAL 不存在，跳过"
    return 0
  fi
  tar czf - -C "$(dirname "$RECIPES_LOCAL")" "$(basename "$RECIPES_LOCAL")" \
    | gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
        -- "sudo su - ubuntu -c 'mkdir -p $(dirname $RECIPES_REMOTE) && cd $(dirname $RECIPES_REMOTE) && tar xzf -'" 2>&1
}

# ── 单组件同步到所有服务器（带条件判断 + 重试） ─────────────────────────────────
sync_component() {
  local component="$1"
  local local_hash
  case "$component" in
    skill)   local_hash=$(local_hash_skill) ;;
    scripts) local_hash=$(local_hash_scripts) ;;
    caw-cli) build_caw_if_needed; local_hash=$(local_hash_caw) ;;
    recipes) local_hash=$(local_hash_recipes) ;;
    *) log "ERROR: unknown component $component"; return 2 ;;
  esac

  log "[$component] local_hash=${local_hash:0:12}..."

  local any_failed=0
  for srv_spec in "${SERVERS[@]}"; do
    IFS=':' read -r name _ _ <<< "$srv_spec"
    local remote_h
    remote_h=$(remote_hash "$srv_spec" "$component")
    local remote_short="${remote_h:0:12}"

    if [[ "$local_hash" == "$remote_h" ]]; then
      log "  [$component][$name] local=$remote_short remote=$remote_short SKIP"
      continue
    fi

    log "  [$component][$name] local=${local_hash:0:12} remote=$remote_short → PUSH"
    local attempt=0
    local pushed=0
    while [[ $attempt -lt 2 ]]; do
      if case "$component" in
          skill)   push_skill   "$srv_spec" ;;
          scripts) push_scripts "$srv_spec" ;;
          caw-cli) push_caw_cli "$srv_spec" ;;
          recipes) push_recipes "$srv_spec" ;;
        esac >/dev/null 2>&1; then
        log "  [$component][$name] PUSH OK"
        pushed=1
        break
      fi
      attempt=$((attempt + 1))
      log "  [$component][$name] PUSH failed, retry $attempt/2"
      sleep 2
    done
    if [[ $pushed -eq 0 ]]; then
      log "  [$component][$name] PUSH FAILED after retries"
      any_failed=1
    fi
  done
  return $any_failed
}

# ── Verify 阶段（独立重算 hash） ──────────────────────────────────────────────
verify_component() {
  local component="$1" local_hash="$2"
  local any_mismatch=0
  for srv_spec in "${SERVERS[@]}"; do
    IFS=':' read -r name _ _ <<< "$srv_spec"
    local remote_h
    remote_h=$(remote_hash "$srv_spec" "$component")
    if [[ "$local_hash" != "$remote_h" ]]; then
      log "  [VERIFY FAIL] [$component][$name] local=${local_hash:0:12} remote=${remote_h:0:12}"
      any_mismatch=1
    else
      log "  [VERIFY OK]   [$component][$name] hash=${remote_h:0:12}"
    fi
  done
  return $any_mismatch
}

# ── 主流程 ───────────────────────────────────────────────────────────────────
log "=== sync_to_servers.sh 启动 ==="
log "component=$COMPONENT servers=${#SERVERS[@]} verify=$VERIFY"

COMPONENTS_TO_SYNC=()
case "$COMPONENT" in
  # recipes 不在 all 里：recipes archive 是 dispatch 时 _run_single_cc_task 在服务器端
  # 每 item 动态写的，本地不应该全量同步（本地和服务器各有自己的 /tmp/caw-eval-recipes）
  all) COMPONENTS_TO_SYNC=(scripts skill caw-cli) ;;
  scripts|skill|caw-cli|recipes) COMPONENTS_TO_SYNC=("$COMPONENT") ;;
  *) log "ERROR: --component 必须是 scripts|skill|caw-cli|recipes|all"; exit 2 ;;
esac

overall_failed=0
for c in "${COMPONENTS_TO_SYNC[@]}"; do
  if ! sync_component "$c"; then
    overall_failed=1
  fi
done

if [[ $overall_failed -ne 0 ]]; then
  log "=== SYNC FAILED for some components ==="
  exit 1
fi

if [[ $VERIFY -eq 1 ]]; then
  log "=== Verify 阶段（独立重算 hash） ==="
  for c in "${COMPONENTS_TO_SYNC[@]}"; do
    local_h=""
    case "$c" in
      skill)   local_h=$(local_hash_skill) ;;
      scripts) local_h=$(local_hash_scripts) ;;
      caw-cli) local_h=$(local_hash_caw) ;;
      recipes) local_h=$(local_hash_recipes) ;;
    esac
    if ! verify_component "$c" "$local_h"; then
      overall_failed=1
    fi
  done
  if [[ $overall_failed -ne 0 ]]; then
    log "=== VERIFY FAILED ==="
    exit 1
  fi
fi

log "=== sync_to_servers.sh 全部完成 ==="
