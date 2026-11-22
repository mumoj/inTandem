# Implementer backends: a neutral layer, and how Codex fits it

Status: proposal, 2026-11-22. Nothing here is built yet.

## Why

inTandem drives one kind of implementer: OpenCode. The goal is to let each
implementer pick its harness (OpenCode, Claude Code, Codex) while the hub,
supervisor, bridge and MCP tools stay the same.

Every implementer already gets its own worktree, branch and supervisor, so
mixing harnesses is a per-implementer choice. What stops it today is that
OpenCode's vocabulary reaches past `intandem/opencode/`:

| Leak | Where |
|---|---|
| `OpenCodeAPI`, `OpenCodeError`, `NotFoundError` | `supervisor/supervisor.py`, `bridge/app.py` |
| OpenCode models (`Session`, `Message`, `Part`, `PermissionRequest`, `ModelRef`, ...) | `supervisor/supervisor.py`, `bridge/schemas.py` |
| OpenCode permission rules (`PermissionRule`, last match wins) | `supervisor/protected.py` |
| OpenCode bus events, matched on raw `type` strings and dug by key (`properties.part.state.status`) | `supervisor/activity.py`, `supervisor/supervisor.py`, `notify.py` |
| Shared OpenCode host lifecycle | `runtime.py`, `hub.py`, `mcp_server.py`, `cli/main.py` |

The last row stays OpenCode-specific on purpose: it is how *that* backend
runs. Only the first four rows are the neutral layer's job.

## Design

A new package, `intandem/backend/`, owns the vocabulary. `intandem/opencode/`
becomes one adapter that speaks it.

```
intandem/backend/
  protocol.py   ImplementerBackend (the Protocol the supervisor depends on)
  models.py     neutral data: SessionInfo, SessionState, TokenUsage, Transcript
                messages, ApprovalRequest, QuestionRequest, FileDiff, ModelChoice
  guard.py      Guard: protected path patterns + "run unattended". No rule syntax.
  events.py     BackendEvent + EventType
  errors.py     BackendError, NotFoundError
intandem/opencode/
  client.py     OpenCodeClient implements ImplementerBackend
  translate.py  OpenCode event/model -> neutral (from_opencode)
  rules.py      Guard -> OpenCode PermissionRule list (moved out of protected.py)
```

Decisions:

1. **The Protocol keeps a pull shape** (`permissions()`, `questions()`,
   `events()`). OpenCode is pull; the Claude Agent SDK and Codex are push. A
   push adapter keeps a pending dict and serves it through the same methods.
   That is less work than turning the supervisor into callback code.
2. **Event type strings stay as they are** (`session.idle`,
   `permission.asked`, `message.part.updated`, ...). They become *inTandem's*
   names that every adapter emits, not OpenCode's. Existing journals, the
   dashboard and `stream_type` keep working unchanged.
3. **Consumers read typed fields, not raw dicts.** `BackendEvent` has
   `session_id`, `parent_session_id`, `request_id`, `status`, `error`,
   `aborted`, `part` (a small neutral part: kind, call id, tool, tool status,
   detail, text field). The adapter does the `dig()`ging. `raw` keeps the
   native line for the stream recorder.
4. **Protection is a `Guard`, enforced by the backend as well as it can, and
   always by the content hash.** `ProtectedFiles.snapshot()/changes()` stay in
   the supervisor because they work for any harness. Only `rules()` moves to
   the OpenCode adapter.
5. **`agent` stays an optional string** passed through to the backend. A
   backend with no agents rejects a non-null value with a clear error.
6. **Subagents get `guard_subagent(session_id, guard, inherited=None)`**
   instead of a general `set_guard`. Guarding a subagent is the only time the
   supervisor changes a live session's policy, and it has to keep the agent's
   own rules. `inherited` carries them opaquely when an event reported them.
   With `None`, the backend looks them up itself.
7. **Neutral models use snake_case field names.** The bridge's `/pending`
   JSON now says `session_id`, not OpenCode's `sessionID`. The CLI, MCP server
   and dashboard all read it through the same models. Evidence files keep
   OpenCode's extra fields as they came.

## Specs

Submit in order. Each one leaves the tree green with no change in behaviour.
Work only after the current uncommitted edits to `hub.py`, `mcp_server.py` and
`docs/overview.svg` are committed.

Checks for both specs:

```
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest
```

### S-B1: neutral backend Protocol and models

> **Outcome.** The supervisor, bridge and protected-files code depend on a new
> `intandem.backend` package instead of `intandem.opencode` for the client
> Protocol, errors and data models. OpenCode behaviour is unchanged.
>
> **Do:**
> - Create `src/intandem/backend/` with `protocol.py` (`ImplementerBackend`:
>   the same methods as today's `OpenCodeAPI`, typed with neutral models),
>   `models.py`, `guard.py` and `errors.py` (`BackendError`, `NotFoundError`).
> - Neutral models carry only the fields something in inTandem reads today.
>   Grep each current OpenCode model's usages to find them. Keep pydantic with
>   `extra="allow"` like `opencode/models.py`.
> - `create_session` and `set_session_permission` take a `Guard` (protected
>   patterns + unattended flag) instead of `list[PermissionRule]`. Rename the
>   latter to `set_guard`.
> - Move `UNATTENDED_RULES` and `ProtectedFiles.rules()` into
>   `src/intandem/opencode/rules.py` as `guard_to_rules(guard)`.
>   `supervisor/protected.py` keeps `snapshot()` and `changes()` and imports
>   nothing from `intandem.opencode`.
> - `OpenCodeClient` satisfies `ImplementerBackend` and converts at its edge.
>   `OpenCodeError` subclasses `BackendError`. OpenCode's `NotFoundError`
>   subclasses the neutral one.
> - Keep `OpenCodeAPI` as an alias of `ImplementerBackend` for one release.
>
> **May touch:** `src/intandem/backend/**` (new),
> `src/intandem/opencode/{client,models,rules}.py`,
> `src/intandem/supervisor/{supervisor,protected}.py`,
> `src/intandem/bridge/{app,schemas}.py`, `tests/fakes.py`, `tests/test_*.py`.
>
> **Must not touch:** `opencode/events.py`, `supervisor/activity.py`,
> `notify.py` (events are S-B2), `hub.py`, `runtime.py`, `mcp_server.py`,
> `cli/`, `docs/`.
>
> **Done when:** the three checks pass, no test is deleted or skipped, and
> `grep -rn "intandem.opencode" src/intandem/supervisor src/intandem/bridge`
> shows only `opencode.events` / `opencode.jsonpath` imports (those go in S-B2).

### S-B2: neutral backend events

> **Outcome.** The supervisor, activity tracker and notifier consume
> `intandem.backend.events.BackendEvent` with typed fields. Nothing outside
> `intandem/opencode/` reads OpenCode's event `properties` by key.
>
> **Do:**
> - Add `src/intandem/backend/events.py`: `EventType` (a `StrEnum` whose values
>   are the strings consumers match today, such as `session.idle`,
>   `permission.asked`, `message.part.updated`, `file.edited` and
>   `server.connected`, plus `other`) and `BackendEvent` (`type`, `native_type`,
>   `session_id`, `parent_session_id`, `request_id`, `status`, `retry_detail`,
>   `error_name`, `error_message`, `aborted`, `part`, `delta_field`, `raw`).
> - Add `src/intandem/opencode/translate.py` with `from_opencode(Event) ->
>   BackendEvent`. All `dig()` calls on OpenCode event payloads move there.
> - `ImplementerBackend.events()` yields `BackendEvent`. The OpenCode client
>   translates. The stream recorder still writes `raw`.
> - `tests/fakes.py`: `event(kind, **properties)` keeps its signature and
>   returns `from_opencode(...)`, so existing tests read the same.
> - The journal's `stream_type` stays the same string as before.
>
> **May touch:** `src/intandem/backend/events.py`,
> `src/intandem/opencode/{events,translate,client}.py`,
> `src/intandem/supervisor/{activity,supervisor}.py`, `src/intandem/notify.py`,
> `tests/**`.
>
> **Must not touch:** `bridge/dashboard.html`, `hub.py`, `runtime.py`,
> `mcp_server.py`, `cli/`, `docs/`.
>
> **Done when:** the three checks pass, no test is deleted or skipped, and
> `grep -rn "intandem.opencode" src/intandem --include=*.py | grep -v
> "^src/intandem/opencode/"` lists only `runtime.py`, `hub.py`,
> `mcp_server.py` and `cli/main.py` (host lifecycle).

### Later (not specced yet)

- `harness` field on `spawn_implementer` / `start_implementer` / `submit_spec`
  (`opencode` by default), and a backend factory in `runtime.py`.
- Diff from `git diff` in the worktree for every backend, instead of each
  harness's own diff.
- Land-time check: `land_implementer` refuses a diff that touches a protected
  path, for backends that cannot block an edit in advance.
- `OpenCode2Backend` (see below), then `ClaudeCodeBackend` (Agent SDK),
  then `CodexBackend`.

## OpenCode 2: a new adapter, not a version bump

Status: built on `feat/opencode-v2`. See "OpenCode 2: as built" below for what changed after
recording a real server.

OpenCode 2.0 rebuilt the server API from scratch ([migration guide](https://opencode.ai/docs/migrate-v1)),
so changing `SUPPORTED_MAJOR` is not enough. From the V2 reference and its
[OpenAPI document](https://opencode.ai/docs/openapi.json) (read in part; field
shapes are **verify**):

| inTandem needs | V1 (today) | V2 |
|---|---|---|
| Routes and scoping | root paths, `directory` | `/api/...`, `location[directory]` query param |
| Health | health route | `GET /api/info` (identity and readiness) |
| Create / update session | ruleset `{permission, pattern, action}` | `POST /api/session` / `PATCH /api/session/{id}` with `permissions`. Config rules are now `{action, resource, effect}` |
| Status map | `session_status()` | No map. `GET /api/session/active`, plus `session.execution.*` events |
| Prompt | prompt with `providerID/modelID`, `agent` | `POST /api/session/{id}/prompt`. Model and agent are set separately (`/model`, `/agent`) |
| Abort | abort | `POST /api/session/{id}/interrupt?resume=` |
| Permissions | list + reply | `/api/session/{id}/permission`, `.../{requestID}/reply` |
| Questions | question routes | Replaced by **forms**: `/api/session/{id}/form`, `.../form/{formID}/reply` |
| Diff | whole session | `GET /api/session/{id}/diff?from=&to=` (per-turn ranges) |
| Events | `/global/event`, `session.idle`, `message.part.*`, `question.*`, `file.edited` | `/api/event`. `session.execution.succeeded/failed/interrupted`, `session.text.*`, `session.tool.*`, `session.usage.updated`, `filesystem.changed`, `permission.asked/replied` |
| Auth | Basic, configurable username | Basic. Username is always `opencode`, password from `OPENCODE_PASSWORD` |

What this means:

- Almost every client call and every event name changes. That is exactly the
  boundary the neutral layer draws: V2 becomes `intandem/opencode2/` (client +
  `from_opencode2` translation) behind `ImplementerBackend`, and the supervisor
  doesn't change. It is also the cheapest real test of S-B1/S-B2, because it
  has the same shape as V1.
- The V2 installer replaces the 1.x binary and has no V1 compatibility mode.
  Until a V2 adapter exists and passes E2E, upgrading the machine breaks
  inTandem. `require_supported` already guards against that.
- The OpenAPI document still calls itself an "Experimental HttpApi surface"
  (`info.version` 0.0.1). Pin a tested 2.x minor range, the way 1.x is pinned.
- Before writing the adapter: install V2 somewhere isolated (a container, or a
  separate `npm` prefix) and save `/doc` (or `openapi.json`) plus a recorded
  event stream as fixtures. Then write the spec against real shapes.

## How Codex fits

Checked against `codex-cli 0.154.0`, using `codex app-server
generate-json-schema` and the [app-server docs](https://learn.chatgpt.com/docs/app-server.md).
Items marked **verify** were not tried live.

**Transport.** `codex app-server` speaks JSON-RPC 2.0 over stdio by default:
newline-delimited JSON, with no `"jsonrpc"` field. Handshake: send
`initialize` with `clientInfo {name, title, version}`, then the `initialized`
notification. There is no official Python client, so inTandem needs a small
asyncio stdio JSON-RPC client (about 200 lines). Pin a supported version range
the way `opencode/server.py` does.

**One process per implementer.** One app-server can host many threads, each
with its own `cwd`, so it could be shared like OpenCode. Start with one
process per implementer anyway: a crash stays isolated, and "health" just
means the process is alive.

| `ImplementerBackend` | Codex app-server |
|---|---|
| `health` | Process alive, and `initialize` succeeded (gives the version) |
| `create_session(title, guard)` | `thread/start {cwd: worktree, model, sandbox, approvalPolicy, developerInstructions}`. Session id = `thread.id` |
| `prompt(text, model, agent)` | `turn/start {threadId, input: [{type: "text", text}], model}`. `agent` is not supported: reject it |
| `abort` | `turn/interrupt {threadId, turnId}`. The adapter tracks the current `turnId` from `turn/started` |
| `session_status` | busy between `turn/started` and `turn/completed`. `retry` when an `error` notification has `willRetry: true` |
| `events` | `turn/completed` (status completed/interrupted/failed) → `session.idle` or `session.error` (interrupted → `aborted`). `item/started` and `item/completed` → `message.part.updated`. `item/agentMessage/delta` → `message.part.delta` |
| `permissions` / `reply_permission` | Server requests `item/commandExecution/requestApproval` and `item/fileChange/requestApproval`. The JSON-RPC request id is the `request_id`. Replies: once → `accept`, always → `acceptForSession`, reject → `decline` (`cancel` also interrupts the turn). `serverRequest/resolved` → `permission.replied` |
| `questions` / `reply_question` | `item/tool/requestUserInput {questions: [{id, header, question, options}], isBlocking}`. Reply `{answers: {<question id>: {answers: [...]}}}`. Marked **experimental**: needs `capabilities.experimentalApi = true` |
| `diff` | `turn/diff/updated` carries a unified diff per turn, and `fileChange` items list `changes[{path, kind, diff}]`. Use `git diff` in the worktree anyway |
| `get_session` cost/tokens | `thread/tokenUsage/updated {total, last}` with input, cached, output and reasoning token counts. No cost field: report 0 on a ChatGPT plan |
| `messages` | `thread/read`, or a transcript the adapter keeps from `item/completed` |
| model listing | `model/list` → `data[{id, displayName, isDefault, ...}]` |

**Protected files: the weak spot.** OpenCode can ask before editing a matching
path. Codex has no path rules: `sandbox` is `read-only`, `workspace-write` or
`danger-full-access`, and `item/fileChange/requestApproval` carries `itemId`
but not the paths. (The paths are on the matching `fileChange` item from
`item/started`.) There are two options:

- **A, strict:** `sandbox: read-only` with `approvalPolicy: on-request`. Every
  file change asks first. The adapter looks up the item's paths, accepts
  unprotected ones itself, and forwards protected ones to the planner as a
  permission. The cost: shell commands that write (test caches, builds) also
  need approval. That could be noisy. **Verify.**
- **B, simple:** `sandbox: workspace-write` with `approvalPolicy: never`. Edits
  inside the worktree just happen, and protection relies on the content-hash
  check (already in `protected.py`) plus the land-time refusal above.

Recommendation: start with B, because it needs no new mechanism. Try A once
the adapter works. `approvalPolicy` also has a `granular` form (`rules`,
`sandbox_approval`, ...) that might sit between the two. **Verify.**

**Auth.** `codex login` with a ChatGPT plan, or an API key. As with Claude:
don't pass a key if you want the plan used. The app-server docs say this auth
is not permitted for commercial or hosted services, which is fine for
personal use.

**Experimental surface to avoid in v1:** `tool/requestUserInput` (questions)
is experimental, and so are `permissions` profiles on `thread/start` and
`process/*`. A v1 Codex adapter can run without questions: answer
`requestUserInput` with an empty answer, and record it so the planner sees it
in the journal.

## OpenCode 2: as built

Recorded against a real 2.3.2 server (released 2026-11-18), driven by the
scripted model, before writing a line of the adapter. The recordings are in
`tests/fixtures/opencode2`.

- **Detection.** 2.x answers every unknown path, `/global/health` included,
  with its web app's HTML. `probe_version` asks `/global/health` for JSON, then
  `/api/info`. The host and each task both read the version from the running
  server, so a host upgraded between runs is driven correctly.
- **Package.** `intandem/opencode2/`: `client.py` (`OpenCode2Client`),
  `rules.py` (`{action, resource, effect}`), `translate.py` (a stateful
  `Translator`, because only a tool call's first event names the tool). The
  runtime picks the client by major version.
- **What the earlier table got wrong or left open:**
  - No session title on create. inTandem's title goes in `metadata.title`.
  - Rules: last match wins, as in 1.x. Verified: `[allow edit *, ask edit
    instruction.md]` stops that edit for a human.
  - Replying to a permission or a form needs its session id. The client
    remembers it from the listing, and looks it up when it doesn't know.
  - Forms: a question's options are field options with a `value`. Answers are
    keyed by field, and a multiselect takes a list.
  - Messages come newest first and paged. The client pages back and reverses.
  - There is no per-directory instance to dispose of, and no status map:
    `/api/session/active` lists the running sessions, and the rest are idle.
  - The tools were renamed: `bash` → `shell`, and `filePath` → `path`.
  - `opencode agent list` is gone. `list_models` asks the server's
    `/api/agent` instead.
- **Install.** No OpenCode on PATH or in `~/.opencode/bin` → inTandem downloads
  release `v2.3.2` for this OS and CPU (x64 baseline without AVX2, musl
  detected) from GitHub, checks it against the release's `SHA256SUMS`, and
  puts it in `~/.opencode/bin`. A custom `INTANDEM_OPENCODE_BIN` is never
  installed. `INTANDEM_NO_INSTALL=1` refuses.
- **Verified:** the full E2E suite passes against 2.3.2
  (`INTANDEM_TEST_OPENCODE=<2.x binary>`) and against 1.18.30 (on PATH).
- **Not done:** subagent sessions in 2.x (the `subagent` tool) were not
  recorded, so `guard_subagent` relies on `session.created` carrying
  `parentID` and `permissions`, as the schema says. The content hash still
  catches a protected edit if that assumption fails.
