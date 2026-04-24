# 公共执行片段（gcloud / SSH / 服务器同步 / Troubleshooting）

本文件收集 `run-eval-cc.md` / `run-eval-openclaw.md` / `run-eval-recipe.md` 共用的环境配置和操作步骤，避免重复。

---

## gcloud Python 兼容性

gcloud ≥ 500 需要 **Python 3.10+**（源码用 PEP 604 `X | None` 语法）。macOS 自带 `/usr/bin/python3` 是 3.9，会报 `unsupported operand type(s) for |: 'type' and 'NoneType'`。用 homebrew 装的 Python：

```bash
# 一次性 export，dispatch 命令执行前必须带
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
# 若无 brew Python，先 brew install python@3.11
```

验证：`CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11 gcloud version` 应正常输出版本号。

**历史兼容（仅升级前适用）**：gcloud ≤ 377 与 Python ≥ 3.12 不兼容（`ModuleNotFoundError: No module named 'imp'`），当时需要指向 `/usr/bin/python3`（3.9）。升级 gcloud 到 565+ 后反转：3.9 不再可用，必须 3.10+。

---

## SSH ControlMaster（避免每次 gcloud IAP 重新认证）

在 Mac `~/.ssh/config` 加：

```
Host *
  AddKeysToAgent yes
  UseKeychain yes
  IdentityFile ~/.ssh/id_rsa
  ControlMaster auto
  ControlPath ~/.ssh/ssh-%C
  ControlPersist 24h
  StrictHostKeyChecking no
```

`gcloud auth login` 一次后，dispatch 并行 SSH 通过 ControlMaster 复用通道，无需多次认证。

---

## 服务器连接信息

```
SSH: gcloud compute ssh --zone "<zone>" "<server>" --tunnel-through-iap --project "<project>"
用户: ubuntu（通过 sudo su - ubuntu 切换）
脚本目录: ~/.agents/skills/caw-eval/scripts/
openclaw:  /home/ubuntu/.npm-global/bin/openclaw
caw:       /home/ubuntu/.cobo-agentic-wallet/bin/caw
```

---

## 服务器同步（dispatch 前必做）

### 前置：拉取最新 master（必做）

同步前先把本地 repo 更新到最新 `origin/master`，避免用过时的 skill/scripts 评测：

```bash
cd <repo>
git pull --rebase origin master
```

**不要对本地未提交修改做 stash/pop**。直接 `git pull`：
- 无冲突：继续下一步
- 有冲突：**停下来**，把冲突文件列表告诉用户让其手动解决（Agent 不替用户解 eval 文件的冲突，因为这些修改通常是用户正在调试的工作副本）

校验：`git rev-list --left-right --count HEAD...origin/master` 应输出 `0\t0`（完全同步）或仅本地领先。落后 origin 时不得进入下一步。

### 推荐：`sync_to_servers.sh`（带 hash 校验 + 条件同步）

```bash
# 导出服务器列表（格式 name:zone:project，空格分隔）
export SERVERS_GPT="srv1:asia-east2-c:my-project srv2:asia-east2-c:my-project"

# 同步所有组件（scripts + skill + caw-cli）并校验
bash sdk/skills/caw-eval/scripts/sync_to_servers.sh --component all --verify --servers-env SERVERS_GPT

# 或指定单组件
bash sdk/skills/caw-eval/scripts/sync_to_servers.sh --component scripts --verify --servers-env SERVERS_GPT
```

特性：
- git tree hash / 内容 sha256 对比本地 vs 远端，**只推 hash 不同的组件**
- Push 后独立 verify 阶段重新算 hash 再比一次，防部分推送失败
- Push 失败自动重试 1 次

### Fallback：手动 tar pipe（无 Go 工具链、无 sync_to_servers.sh 时）

```bash
export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
REPO=~/etl/cobo-agent-wallets

for spec in "${SERVERS[@]}"; do
  IFS=':' read -r name zone project <<< "$spec"
  tar czf - \
    -C "$REPO/cobo-agent-wallet/sdk/skills" \
    --exclude='caw-eval/scripts/__pycache__' \
    caw-eval cobo-agentic-wallet-sandbox \
    | gcloud compute ssh --zone "$zone" "$name" --tunnel-through-iap --project "$project" \
        -- "sudo su - ubuntu -c 'mkdir -p ~/.agents/skills && cd ~/.agents/skills && tar xzf -'" &
done
wait
```

注意：`scripts/.env`（Langfuse 凭证）不在 git 中，本地 scripts/ 通常不含 `.env`，不会覆盖服务器上的配置。

---

## 新服务器初始配置清单

新建 openclaw 服务器后，**第一次**跑评测前需完成：

```bash
# 1. SSH 进服务器
gcloud compute ssh --zone "$ZONE" "$SERVER" --tunnel-through-iap --project "$PROJECT" \
  -- "sudo su - ubuntu"

# 2. 系统 python 可能不带 pip
sudo apt-get update && sudo apt-get install -y python3-pip

# 3. 依赖（langfuse 必须 pin 4.0.6，4.2.0 移除了 Langfuse.api）
pip3 install --user --break-system-packages python-dotenv "langfuse==4.0.6"

# 4. 配 .env（Langfuse 凭证），从本地 scp 或手动填
mkdir -p ~/.agents/skills/caw-eval/scripts/
# gcloud compute scp --zone "$ZONE" --project "$PROJECT" --tunnel-through-iap \
#   ~/.agents/skills/caw-eval/scripts/.env \
#   ubuntu@"$SERVER":~/.agents/skills/caw-eval/scripts/.env
```

服务器搭建完整流程（GCP 实例创建 / onboarding / 充值）：[server-setup.md](./server-setup.md)

---

## Troubleshooting 速查表

| 问题 | 解决 |
|------|------|
| `gcloud` 报 `unsupported operand type(s) for \|: 'type' and 'NoneType'` | gcloud ≥ 565 需 Python 3.10+，`export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11`（本文"gcloud Python 兼容性"段） |
| `gcloud` 报 `No module named 'imp'`（仅 gcloud ≤ 377） | 已废弃场景：升级 gcloud 到 565+ 并用 Python 3.10+ |
| 某台 IAP 连接失败 | 单独跑一次 `gcloud compute ssh ...` 确认，必要时 `gcloud auth login` |
| `AttributeError: 'Langfuse' object has no attribute 'api'` | 服务器 langfuse 版本过新：`pip3 install --user --break-system-packages "langfuse==4.0.6"` |
| `Agent "eval-xxx" already exists` | 上次异常残留。脚本已内置预清理；手动：`openclaw agents delete eval-xxx --force` |
| `Loaded 0 judge result(s)` | `judge_results.json` 每条必须含 `trace_id` **和** `item_id`，缺失需从 `judge_req.json` 补齐再合并 |
| `openclaw: command not found` | SSH 中确认 PATH 包含 `/home/ubuntu/.npm-global/bin` |
| `pip install` 报 PEP 668 错误 | Debian 系统保护：加 `--break-system-packages` |
| dispatch 日志为空但 Langfuse 有 trace | IAP tunnel stdout 缓冲。数据已上传；可用 `--fire-and-forget` + nohup 避免 |
| SSH 阻塞等待过久 | 用 `--fire-and-forget` + `--watch` 流水线模式，dispatch 立即返回 |
| 单 task 超时 | 加 `--timeout 900`（默认 600） |
| 部分 item 失败 | dispatch 会输出失败项和重跑命令，`--item-id` 重跑 |
| SETH 余额不足 | 从余额充裕的服务器用 `openclaw agent` 转入，或 `caw faucet` |
| USDC 余额不足 | 参考 `run-eval-openclaw.md` 的"余额预检"章节里的并行 swap 脚本 |
| precheck 失败但本地无未 commit 改动 | `sync_to_servers.sh --component all --verify` 再跑一次；实在不行 `--force`（会标记 run 不可信） |
