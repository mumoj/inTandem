# Implementer backends: neutral layer, OpenCode 1/2, Claude Code, and Codex fit

Status: **implemented** (`intandem/backend/`, `intandem/opencode/`, `intandem/opencode2/`,
`intandem/claude/`). Only **Codex** (and optional land-time protected-path refusal) remains
unbuilt.

## Why

inTandem originally drove one implementer harness: OpenCode 1.x. Because every implementer
already gets its own worktree, branch, and supervisor, mixing harnesses is naturally a
per-implementer choice. Separating the supervisor, bridge, activity tracker, and notifier from
OpenCode's wire shapes lets OpenCode 1.x, OpenCode 2.x, Claude Code, and future harnesses (Codex)
plug into the same hub and MCP tools.

## Neutral layer (`intandem/backend/`) — as built

```
intandem/backend/
  protocol.py   ImplementerBackend (pull-shaped Protocol the supervisor depends on)
  models.py     neutral pydantic models (Session, SessionStatus, Message, Part,
                PermissionRequest, QuestionRequest, FileDiff, TokenUsage, ModelRef)
  guard.py      Guard: protected path patterns + unattended flag (no rule syntax)
  events.py     BackendEvent, EventType (StrEnum), PartUpdate, ASKED/SETTLED/TURN_OVER
  errors.py     BackendError, NotFoundError
  jsonpath.py   dig / dig_str / dig_list shared across adapters
  stream.py     StreamRecorder (raw stream writer with oversized-line stubbing)
  diff.py       git_head / git_diff reusable worktree diff helpers
```

Key design choices:

1. **Pull-shaped Protocol (`permissions()`, `questions()`, `events()`).** OpenCode is pull;
   Claude Agent SDK and Codex are push. Push adapters park futures in a pending dict and push
   `BackendEvent`s into a queue, keeping the supervisor free of callback re-entrancy.
2. **Event type strings stay stable (`session.idle`, `permission.asked`, `message.part.updated`,
   ...).** They are now *inTandem's* `EventType` values, so existing journals, `stream_type`, and
   the dashboard work identically across harnesses.
3. **Typed fields on `BackendEvent`.** Consumers never `dig()` into raw dicts. `PartUpdate`
   normalizes tool state to `running: bool` and carries a bounded `detail`. `native` carries the
   harness payload for `$INTANDEM_EVENT` alerts (populated lazily on alert events in OpenCode 1.x),
   and `raw` carries the native line for `StreamRecorder`.
4. **Protection is a `Guard`, always backed by content hashes.** Each backend enforces `Guard`
   as best it can (OpenCode rulesets, Claude `PreToolUse` hook); `ProtectedFiles.snapshot()` /
   `changes()` remain in the supervisor for every harness.
5. **Subagents use `guard_subagent(session_id, guard, inherited=None)`.** OpenCode appends `deny`
   rules onto the subagent's existing rules (fetching them if `inherited` is `None`); Claude Code
   treats it as a no-op because subagents share the session's `PreToolUse` hook.
6. **Neutral `ModelRef` and request IDs.** `ModelRef` accepts both `provider/model` and bare model
   names (`claude-sonnet-4-6`), letting OpenCode enforce `provider/model` while Claude Code (and
   future Codex) accept bare names. Bridge `PermissionId` and `QuestionId` accept any path-safe
   token rather than OpenCode-only prefixes.
7. **Lazy shared host.** `Hub` starts the shared `opencode serve` lazily on the first OpenCode
   task (or `list_models()` for OpenCode), so Claude-only sessions never install or boot OpenCode.

## OpenCode 1.x (`intandem/opencode/`) — as built

- `client.py`: `OpenCodeClient` implements `ImplementerBackend`, converts camelCase wire models
  at its edge while preserving extra fields for evidence, and enforces `provider/model`.
- `translate.py`: `from_opencode(Event) -> BackendEvent`; lazily serializes `native` only for
  alert events (`PERMISSION_ASKED`, `QUESTION_ASKED`, `ERROR`).
- `rules.py`: `guard_to_rules(guard)` and `subagent_rules(inherited, guard)` (last match wins).
- `server.py` / `host.py` / `install.py`: probes `/global/health` then `/api/info` to detect 1.x
  vs 2.x; sets both `OPENCODE_SERVER_PASSWORD` and `OPENCODE_PASSWORD`; downloads checksum-verified
  OpenCode `2.3.2` into `~/.opencode/bin` when `opencode` is missing (unless `INTANDEM_NO_INSTALL`
  is set or a custom `INTANDEM_OPENCODE_BIN` was given).

## OpenCode 2.x (`intandem/opencode2/`) — as built

Recorded against a real `2.3.2` server driven by `tests/scripted_llm.py` (fixtures in
`tests/fixtures/opencode2`). Full E2E suite passes against both `2.3.2` and `1.18.30`.

- **Detection.** 2.x returns HTML for unknown paths including `/global/health`. `probe_version` /
  `aprobe_version` require `application/json` on `/global/health`, then fall back to `/api/info`.
  `runtime.make_backend` picks `OpenCodeClient` or `OpenCode2Client` from the server's major version.
- **Package (`intandem/opencode2/`):**
  - `client.py` (`OpenCode2Client`): `/api/...` routes, `location[directory]` scoping, `metadata.title`
    on create, sequential `/model` + `/agent` + `/prompt`, session-scoped permission/form replies
    (remembering `request_id -> session_id` with lazy lookup), form-to-question translation,
    paginated reverse-order message fetching (`order=desc` preserved across pages), and
    `metadata["diff"]` mapped for the dashboard.
  - `rules.py`: `{action, resource, effect}` ruleset (last match wins, verified on `2.3.2`).
  - `translate.py`: stateful `Translator` remembering tool call names and input summaries between
    `session.tool.input.started` and `session.tool.success`/`failed`.
- **CLI & MCP:** `list_models()` awaits `hub.ready_opencode()`, uses `host.binary` (working with
  auto-installed binaries), and queries `/api/agent` on 2.x (which dropped `opencode agent list`).
  `inTandem attach` passes both `OPENCODE_SERVER_PASSWORD` and `OPENCODE_PASSWORD`.
- **Open note:** 2.x subagent sessions (`subagent` tool) were not recorded live; `guard_subagent`
  relies on `session.created` carrying `parentID` and `permissions` as documented, with the
  content-hash check as backstop.

## Claude Code (`intandem/claude/`) — as built

Optional `claude` extra (`claude-agent-sdk>=0.2.187,<0.3`), imported lazily inside
`runtime.make_backend`. Picked via `harness="claude"` on `start_implementer`, `spawn_implementer`,
`submit_spec`, `inTandem serve --harness claude`, or `INTANDEM_HARNESS=claude`.

- **Auth.** Runs the user's own `claude` CLI on `PATH` (or `INTANDEM_CLAUDE_BIN`) as `cli_path`,
  using `claude auth login` (subscription). Never sets `ANTHROPIC_API_KEY`; logs a
  `billing_warning` in the journal if that env var is present.
- **Mapping:**
  - `health`: cached `claude --version` (fails fast at task start if CLI is missing).
  - `create_session`: allocates `ses_<hex>` + a UUID for SDK `session_id`, records `git_head`, and
    disconnects older sessions' CLIs (bumping a per-session `epoch` under `session.lock` so an
    in-flight `connect()` cannot query a retired or aborted session).
  - `prompt`: accepts bare model names or `anthropic/<model>`, rejects non-Anthropic providers and
    non-null `agent`, connects one `ClaudeSDKClient` per session (`permission_mode="default"`),
    switches models with `client.set_model`, and calls `client.query`.
  - `events`: translates `AssistantMessage`, `UserMessage` (`tool_result` + `FILE_EDITED` for edit
    tools), `StreamEvent` deltas, `ResultMessage`, and `RateLimitEvent` into `BackendEvent`s.
  - `permissions` / `questions`: `PreToolUse` hook (`on_failure="block"`, 7-day timeout) intercepts
    edits to protected paths before user `permissions.allow` rules can allow them; `can_use_tool`
    re-checks protected paths, handles `AskUserQuestion` (`updated_input={"answers": ...}`), and
    allows or parks other tools depending on `Guard.unattended`. Permission metadata includes
    `diff` (for edits/writes) and `command` (for Bash) so the dashboard shows previews.
  - `diff`: uses shared `backend/diff.py` (`git_diff` against session-start `HEAD` + untracked).
  - `guard_subagent`: no-op (subagents run inside the same CLI session and hit the same hook).
- **Limits:**
  - Sessions do not survive a supervisor restart (supervisor detects missing session and
    redelivers the spec to a new session).
  - Shell writes to protected files are caught after the fact by the content hash, as in OpenCode.
  - `AskUserQuestion` via `can_use_tool` and cumulative `total_cost_usd` were verified against the
    SDK source and unit tests; `tests/test_claude_e2e.py` (`INTANDEM_E2E_CLAUDE=1`) smoke-tests a
    live turn and protected edit.

## Antigravity CLI (`intandem/antigravity/`) — as built

No extra dependency: a stdlib asyncio JSON-RPC 2.0 client over `agy acp`'s stdio (ACP, the
Agent Client Protocol, newline-delimited JSON). Picked via `harness="antigravity"`,
`inTandem serve --harness antigravity` (`--antigravity-bin`), or `INTANDEM_HARNESS=antigravity`.
Wire shapes were recorded from `agy acp` 1.5.14.

- **Transport (`acp.py`).** `StdioACPTransport` runs `[cli, "acp"]` in the task directory with a
  64 MiB line limit, keeps the last 200 stderr lines for crash messages, answers agent requests
  in tasks of their own, and delivers notifications inline (in order). Tests inject an in-memory
  `ACPTransport` through `TransportFactory`.
- **One process per backend** (one per task), started by `health()` at task start and closed on
  `__aexit__`. A dead process fails `health()` (so `watch_host` stops the task); the next
  `create_session` starts a fresh one.
- **Mapping:**
  - `health`: `initialize {protocolVersion: 1, clientCapabilities: {}}` → `agentInfo` version.
  - `create_session`: `session/new {cwd, mcpServers: []}`; the ACP `sessionId` is inTandem's id.
    Records `git_head`, the session's modes and current model, and `session/close`s older ones
    (their transcripts and totals stay readable).
  - `prompt`: bare agy model IDs or `antigravity/`/`google/<model>` → `session/set_model`; other
    providers are refused. `agent` is an agy mode (`default`, `accept-edits`, `auto`, `plan`) →
    `session/set_mode`. `session/prompt` runs in a background task; its result's `usage`
    (`thoughtTokens` → `reasoning`, `cachedReadTokens`/`cachedWriteTokens` → `cache`) is added up,
    then `IDLE` (or `ERROR`, aborted on `stopReason: "cancelled"`).
  - `abort`: `session/cancel` notification, and pending permissions answered `cancelled`.
  - `events`: `agent_thought_chunk`/`agent_message_chunk` → `PART_DELTA` (`reasoning`/`text`),
    `tool_call`/`tool_call_update` → tool `PART_UPDATED`, plus `FILE_EDITED` when an edit
    completes; other updates are `OTHER`.
  - `permissions`: `session/request_permission`. A protected edit (`kind: "edit"` or an edit tool
    name; paths from `locations`, `rawInput`, `args`, diff content) is always parked, even
    unattended. Other tools are allowed at once when unattended, otherwise parked. Replies pick
    `allow_once` (also for "always", which inTandem remembers itself: an agent-side
    `allow_always` could wave a later protected edit through) or `reject_once`. Metadata carries
    `diff` and `command` previews.
  - `questions`: none (ACP has no question request). `guard_subagent`: no-op.
- **Limits:** modes such as `auto` and `accept-edits` may stop agy asking before edits, and shell
  writes never ask; the content hash (run at once on `FILE_EDITED`) catches both. A
  rejection's message is not passed to the agent (ACP has no field for it). Sessions do not
  survive a supervisor restart. `tests/test_antigravity_e2e.py` (`INTANDEM_E2E_ANTIGRAVITY=1`)
  smoke-tests a live turn.

## Next: How Codex fits (not built yet)

Checked against `codex-cli 0.154.0`, using `codex app-server generate-json-schema` and the
[app-server docs](https://learn.chatgpt.com/docs/app-server.md). Items marked **verify** were not
tried live.

**Transport.** `codex app-server` speaks JSON-RPC 2.0 over stdio (newline-delimited JSON without
`"jsonrpc"`). Handshake: `initialize` with `clientInfo {name, title, version}`, then `initialized`
notification. Needs a small asyncio stdio JSON-RPC client (~200 lines) with version pinning.

**One process per implementer.** Start with one `codex app-server` per task for crash isolation.

| `ImplementerBackend` | Codex app-server |
|---|---|
| `health` | Process alive and `initialize` succeeded (gives the version) |
| `create_session(title, guard)` | `thread/start {cwd: worktree, model, sandbox, approvalPolicy, developerInstructions}`. Session id = `thread.id` |
| `prompt(text, model, agent)` | `turn/start {threadId, input: [{type: "text", text}], model}`. `agent` not supported: reject |
| `abort` | `turn/interrupt {threadId, turnId}` (track current `turnId` from `turn/started`) |
| `session_status` | busy between `turn/started` and `turn/completed`; `retry` when `error` has `willRetry: true` |
| `events` | `turn/completed` → `IDLE` or `ERROR` (`interrupted` → `aborted`); `item/started` + `item/completed` → `PART_UPDATED` (plus `FILE_EDITED` on `fileChange` completion); `item/agentMessage/delta` → `PART_DELTA` |
| `permissions` / `reply_permission` | Server requests `item/commandExecution/requestApproval` and `item/fileChange/requestApproval`. Replies: `once` → `accept`, `always` → `acceptForSession`, `reject` → `decline` (`serverRequest/resolved` → `PERMISSION_REPLIED`) |
| `questions` / `reply_question` | `item/tool/requestUserInput` (marked **experimental**; needs `capabilities.experimentalApi = true`). In v1, can decline/answer empty and record in journal |
| `diff` | Shared `backend/diff.py` (`git_diff` in the worktree) |
| `get_session` cost/tokens | `thread/tokenUsage/updated {total, last}`; no cost field (report 0 on ChatGPT plan) |
| `messages` | `thread/read` (needed for restart recovery via `_session_contains`, plus `thread/resume` on restart — **verify**) |
| model listing | `model/list` → `data[{id, displayName, isDefault, ...}]` |

**Protected files (the weak spot for Codex).** Codex has no path-based rules:

- **A, strict:** `sandbox: read-only` + `approvalPolicy: on-request`. Adapter correlates `itemId`
  with `fileChange` paths from `item/started`, auto-accepts unprotected edits, and forwards
  protected ones. Likely noisy for commands that write build/test caches. **Verify.**
- **B, simple (start here):** `sandbox: workspace-write` + `approvalPolicy: never`. Rely on
  `ProtectedFiles` content-hash checks (triggered immediately on `FILE_EDITED`) plus a land-time
  protected-path refusal in `land_implementer`. `approvalPolicy`'s `granular` form may sit
  between A and B (**verify**).
