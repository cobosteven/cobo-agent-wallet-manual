# Onboarding

## 1. Install caw

Run `./scripts/bootstrap-env.sh --env sandbox --only caw` to install caw. caw → `~/.cobo-agentic-wallet/bin/caw`; add that dir to PATH. TSS Node is downloaded automatically during onboard when needed.

**Prerequisites:** `python3` and `node` / `npm` (for DeFi calldata encoding). Install Node.js if absent: https://nodejs.org. Several recipes also require `ethers`: `npm install ethers`.

## 2. Onboard

`caw onboard` is interactive by default — it walks through mode selection, credential input, waitlist, and wallet creation step by step via JSON prompts. Each call returns a `next_action` telling you the exact next step; follow it until `wallet_status` becomes `active`.

```bash
export PATH="$HOME/.cobo-agentic-wallet/bin:$PATH"
caw --format json onboard --create-wallet --env sandbox
```

If the user already has a token or invitation code **before starting**, pass it directly on the **first call** to skip mode selection and credential prompts:

```bash
# Token from the Web Console — human-owned wallet, full functionality from the start
caw --format json onboard --create-wallet --env sandbox --token <TOKEN>

# Invitation code from Cobo — you own the wallet initially, with limited functionality.
# Your owner can claim the wallet later to unlock full functionality (see Claiming below).
caw --format json onboard --create-wallet --env sandbox --invitation-code <CODE>
```

> **CRITICAL:** The shortcut commands above are for the **first call only**. Once you have called `caw onboard` and received a `session_id`, you **MUST** include `--session-id <SESSION_ID>` on **every** subsequent call — even when adding `--invitation-code` or `--token`. Omitting `--session-id` starts a brand-new session, discarding prior progress and TSS prewarm work.

**How the interactive loop works:**
1. Call `caw onboard` — read `phase`, `prompts`, `needs_input`, `next_action`, and `session_id`.
2. On each follow-up, pass `--session-id` with the **latest** `session_id` from the previous response, and keep the same `--create-wallet` and `--env` as the initial call. If the response says the session was not found and a new one was created, use that **new** `session_id`.
3. When `needs_input` is true, pass `--answers` as JSON whose keys match `prompts[].id` (e.g. `onboard_mode`: `option_a` or `option_b`; later `invitation_code`, `agent_name`, etc., depending on phase).
4. Repeat until onboarding finishes — typically `wallet_status` is `active` and/or `phase` is `wallet_active`. If input is invalid, use `last_error` and resubmit with corrected `--answers`.
5. If the background bootstrap worker fails or stops (`phase` is `error`, or `next_action` mentions `--retry-bootstrap`), follow that `next_action` exactly. Typically you re-run onboard with **`--retry-bootstrap`**, the **same `--session-id`**, and the same **`--create-wallet` / `--env`** (and `--api-url` if you used it).

Example follow-up call:

```bash
caw --format json onboard --session-id <SESSION_ID> --answers '{"security_ack":true}'
```

Use `phase` + `bootstrap_stage` + `wallet_status` to track progress.

**Assistants / LLM agents:** When `needs_input` is true, read `prompts` and present each question to the **user**; only pass `--answers` with keys matching the current prompt `id` values after you have their input. **Do not** pass `{"skip_phase":true}` unless the user explicitly asks to skip that optional step—`skip_phase` completes the pending phase without collecting those answers, which is only for explicit opt-out.

When `needs_input` is false, **immediately show the `message` to the user** and follow `next_action` (for wallet activation, the CLI usually suggests polling about every 10 seconds — use the exact interval in `next_action` if it differs). Do not analyze or deliberate on the response — just relay the message and execute the next action.

Without `--session-id`, starts a new onboarding. With `--session-id <SESSION_ID>`, resumes that session.
If the provided `--session-id` does not exist, the CLI creates a new session automatically.

**Non-interactive mode:** For scripted / CI usage, add `--non-interactive` to get the legacy synchronous behavior (requires `--token` and/or `--create-wallet`).

See [Error Handling](./error-handling.md#onboarding-errors) for common onboarding errors.

## Environment

| Environment | `--env` value | API URL                                          | Web Console |
|-------------|---------------|--------------------------------------------------|-------------|
| Sandbox | `sandbox` | `https://api-core.agenticwallet.sandbox.cobo.com` | https://agenticwallet.sandbox.cobo.com/ |

Set the API URL before any command:

```bash
export AGENT_WALLET_API_URL=https://api-core.agenticwallet.sandbox.cobo.com
```

## Claiming — Transfer Ownership to a Human

When the user wants to claim a wallet (e.g., "我要 claim 这个钱包", "claim the wallet"), use these commands:

```bash
caw profile claim                   # generate a claim link
caw profile claim-info              # check claim status
```

`claim` returns a `claim_link` URL. Share this link with the user — they open it in the Web Console to complete the ownership transfer. Once claimed, the wallet becomes human-owned with full functionality (Cobo Gasless sponsorship becomes available via `--gasless`).

Use `claim-info` to check the current state: `not_found` (no claim initiated), `valid` (pending, waiting for the user), `expired`, or `claimed` (transfer complete).

**Claim status tracking (profile claim only)**: `--context` is **required** for `caw profile claim`. When openclaw notification context is available, pass `channel`, `target`, `session_id`, and `prompt`. **`session_id`**: UUID string identifying the current openclaw conversation session — read it from `openclaw sessions --json --agent <agent>` or equivalent method. If notification context is not available, pass `--context '{}'`. When the terminal notification is delivered (claimed/expired), continue any established next steps from the conversation context. Use: `--context '{"channel":"<channel>", "target":"<target>", "session_id":"<uuid>", "prompt":"include the status update and a next-step execution plan based on the conversation context. Prefix the response with [Notification] to distinguish it from normal conversation replies"}'` — **this prompt applies to `caw profile claim` only.** 

## Multi-wallet Setup

Each `caw onboard` creates a separate wallet with its own credentials and TSS Node files. Multiple wallets can coexist on one machine, which is useful when an agent serves different purposes (e.g. one for DeFi execution, another for payroll disbursements).

- **Default wallet**: Most commands automatically use the active wallet. Switch it with `caw profile use <agent_id>`.
- **`--profile` flag**: Any command accepts `--profile <agent_id>` to target a specific wallet without changing the default. Use this when running multiple agents concurrently.
- **After onboarding**: Record the `agent_id` in AGENTS.md (or equivalent project instructions file) so future sessions know which wallet to use.

```bash
# Example: transfer using a non-default wallet
caw --profile caw_agent_abc123 tx transfer --to 0x... --token SOLDEV_SOL_USDC --amount 0.0001
```

See `caw profile --help` for all subcommands (`list`, `current`, `use`, `env`, `archive`, `restore`).

> **ONLY use archive when a previous onboarding has failed and you need to retry.** Do NOT archive before a fresh onboarding — the `onboard` command creates a new wallet automatically.
