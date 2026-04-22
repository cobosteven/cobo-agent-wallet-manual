#!/usr/bin/env bash
# Fan-out bootstrap_cc_server.sh 到多台 GCE 服务器（并行）。
#
# Usage:
#   export ANTHROPIC_AUTH_TOKEN=sk-...
#   export ANTHROPIC_BASE_URL=https://sub2api-gcp.1cobo.com    # 可选，有默认
#   export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
#   bash fan_out_bootstrap.sh \
#     luochong-openclew-dev-v1-20260415-0253420-test1:asia-east2-c:openclaw-keq9xwm4 \
#     luochong-openclew-dev-v1-20260415-025458-test2:asia-east2-c:openclaw-keq9xwm4 \
#     ...
#
set -euo pipefail

: "${ANTHROPIC_AUTH_TOKEN:?export ANTHROPIC_AUTH_TOKEN first}"
: "${ANTHROPIC_BASE_URL:=https://sub2api-gcp.1cobo.com}"

if [ $# -eq 0 ]; then
  echo "usage: $0 <name:zone:project> [<name:zone:project> ...]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOTSTRAP_SRC="$SCRIPT_DIR/bootstrap_cc_server.sh"
[ -f "$BOOTSTRAP_SRC" ] || { echo "missing $BOOTSTRAP_SRC"; exit 1; }

LOG_DIR="/tmp/caw-eval-cc-bootstrap-$(date +%s)"
mkdir -p "$LOG_DIR"
echo "日志目录: $LOG_DIR"

run_one() {
  local spec="$1"
  local log="$LOG_DIR/$(echo "$spec" | cut -d: -f1).log"
  local name zone project
  IFS=':' read -r name zone project <<< "$spec"

  {
    echo "=== [$name] bootstrap 开始 ==="
    # 1. 推 bootstrap 脚本
    gcloud compute scp \
      --zone "$zone" --project "$project" --tunnel-through-iap \
      "$BOOTSTRAP_SRC" "$name:/tmp/bootstrap_cc_server.sh" \
      || { echo "[$name] scp 失败"; exit 1; }

    # 2. 远端执行（env 通过 shell 环境变量传入 ubuntu 用户）
    gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
      --ssh-flag="-o ServerAliveInterval=60" \
      -- "sudo su - ubuntu -c 'ANTHROPIC_AUTH_TOKEN=\"$ANTHROPIC_AUTH_TOKEN\" ANTHROPIC_BASE_URL=\"$ANTHROPIC_BASE_URL\" bash /tmp/bootstrap_cc_server.sh'"
    local rc=$?
    echo "=== [$name] exit rc=$rc ==="
    exit $rc
  } > "$log" 2>&1
}

PIDS=()
for spec in "$@"; do
  run_one "$spec" &
  PIDS+=($!)
done

FAIL=0
for i in "${!PIDS[@]}"; do
  wait "${PIDS[$i]}" || {
    echo "[FAIL] ${@:$((i+1)):1}"
    FAIL=$((FAIL+1))
  }
done

echo
echo "=== 汇总 ==="
for spec in "$@"; do
  name=$(echo "$spec" | cut -d: -f1)
  if grep -q "bootstrap 完成" "$LOG_DIR/$name.log" 2>/dev/null; then
    echo "  $name: OK"
  else
    echo "  $name: FAIL — 看日志 $LOG_DIR/$name.log"
  fi
done

[ "$FAIL" -eq 0 ] && echo "全部成功" || { echo "$FAIL 台失败"; exit 2; }
