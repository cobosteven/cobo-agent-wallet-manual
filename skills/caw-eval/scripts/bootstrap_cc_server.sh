#!/usr/bin/env bash
# Bootstrap a GCE server for headless Claude Code eval:
#   1. 写 ~/.claude_code.env（需 env vars ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL）
#   2. npm 装 @anthropic-ai/claude-code
#   3. 验证 sandbox skill 存在（复用 openclaw 已装的 cobo-agentic-wallet-sandbox）
#
# Usage: 以 ubuntu 用户跑；fan-out 时由 fan_out_bootstrap.sh 通过 gcloud ssh 触发。
#
set -euo pipefail

: "${ANTHROPIC_AUTH_TOKEN:?Need ANTHROPIC_AUTH_TOKEN (sk-...)}"
: "${ANTHROPIC_BASE_URL:=https://sub2api-gcp.1cobo.com}"

export PATH=/home/ubuntu/.npm-global/bin:/home/ubuntu/.cobo-agentic-wallet/bin:$PATH

echo "=== [1/4] 写 ~/.claude_code.env ==="
cat > ~/.claude_code.env <<EOF
export ANTHROPIC_AUTH_TOKEN="$ANTHROPIC_AUTH_TOKEN"
export ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
EOF
chmod 600 ~/.claude_code.env
ls -la ~/.claude_code.env

echo "=== [2/4] 装 claude CLI ==="
if command -v claude >/dev/null 2>&1; then
  echo "claude already installed: $(claude --version 2>&1 | head -1)"
else
  npm install -g @anthropic-ai/claude-code 2>&1 | tail -5
  claude --version 2>&1 | head -1
fi

echo "=== [3/4] 验证 sandbox skill ==="
if [ -d "$HOME/.agents/skills/cobo-agentic-wallet-sandbox" ]; then
  head -5 "$HOME/.agents/skills/cobo-agentic-wallet-sandbox/SKILL.md"
else
  echo "[WARN] sandbox skill 不存在，尝试 npx skills add..."
  npx -y skills add cobosteven/cobo-agent-wallet-manual \
    --skill cobo-agentic-wallet-sandbox --yes --global 2>&1 | tail -5
fi

echo "=== [4/4] headless 健康检查 ==="
# shellcheck disable=SC1090
source ~/.claude_code.env
claude -p --output-format text "reply exactly: OK" 2>&1 | tail -1

echo "=== bootstrap 完成 ==="
