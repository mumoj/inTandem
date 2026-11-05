# Using inTandem

## Connect it to a planner

Register inTandem as an MCP server for Claude Code or Codex:

```bash
claude mcp add inTandem --scope user -- inTandem mcp      # Claude Code
codex mcp add inTandem -- inTandem mcp                    # Codex
```

After registration, the planner can delegate a task through inTandem while retaining
responsibility for the specification and review. For example, ask it to have an implementation
model write tests for `citations.py`. It uses tools such as `submit_spec`, `wait`,
`get_result`, `get_pending`, `reply_permission`, `send_directive`, and `finish` to submit work,
monitor it, and review the result.

### Session lifecycle

When the planner connects, `inTandem mcp` starts one OpenCode server in the background. The
first `submit_spec` starts a supervisor for the planner's project. Use `start_implementer` only
to set a default model or protected files.

The MCP process and its OpenCode child exist for the planner session. When the planner
disconnects or exits, supervisors finish and preserve their evidence, then OpenCode stops. Task
state remains on disk and can be resumed in a later session.

### Models and subagents

Each `submit_spec` can set a `model` (`provider/model`) and `agent` (`build`, `plan`, or an agent
from your OpenCode configuration). Use `list_models` to see the models OpenCode reports.

If an implementer delegates to an OpenCode subagent, that subagent's permission requests also
block the spec. Its activity keeps the spec active, and protected-file rules apply to its
session as well.

For independent parts of a task, the planner can call `spawn_implementer` once for each part.

## Parallel implementers

Use parallel implementers when a task has independent pieces. Each one has a name, a separate
worktree, and its own supervisor, so its changes can be reviewed, merged, or discarded
independently:

```text
spawn_implementer(name="farewell", text=..., model="openai/gpt-5.6-luna")
spawn_implementer(name="subtract", text=..., model="openai/gpt-5.6-luna")
wait_any()                                   # wakes when either one needs the planner
land_implementer(name="farewell")            # review: the diff against where it forked
land_implementer(name="farewell", action="merge")
```

Each implementer works on a dedicated `intandem/<name>` branch in a Git worktree under the
state root, outside the repository. It has its own supervisor, journal, and protected-file
configuration; the planner's working tree remains unchanged.

`wait_any` returns when any implementer finishes, blocks, or stops, and identifies every
implementer that needs attention. While it waits, it sends MCP progress messages such as
`farewell finished S-1: result`.

Pass the implementer's name as `workdir` to address it with other tools:
`get_pending(workdir="farewell")` and `send_directive(..., workdir="farewell")`, for example.

Landing is explicit. `review` shows committed and uncommitted changes relative to the fork
point. `merge` commits the implementer's changes and merges them into the current branch. If
the merge conflicts, it is aborted and the repository is left unchanged. `discard` removes the
work instead. Both operations stop the supervisor, remove the worktree and branch, and retain
state under `landed/`. Files created by checks, such as `__pycache__`, are not committed.

Parallel implementers share [one process and one OpenCode server](architecture.md#one-process-one-opencode).
Once OpenCode is running for the session, additional implementers reuse it.

## Monitor work in a browser

Every MCP response includes a `dashboard` link and an `overview` link for the session's
implementers. `inTandem serve` prints the dashboard link when it starts. The dashboard shows the
implementer's current activity, why `wait` would return, recent messages, results, changed
files, spend, and a journal timeline.

Permission requests and questions include controls to allow, deny, or answer them. This lets a
person resolve a request without opening a terminal. **Open in OpenCode** opens OpenCode's web
interface on the same server. The access token is stored in the link's `#fragment`, which
browsers do not send to the server.

## Use the command line

Planners normally use the MCP tools. The CLI calls the same bridge, so a person or script can
step in without creating a separate view of the task. It is useful for:

- Manual intervention from a terminal or over SSH.
- Scripts and CI, where [exit codes](#exit-codes) identify the result and `--json` returns the raw response.
- Planners that can run shell commands but do not support MCP.
- Starting a task directly with `inTandem serve`, outside a planner session.

To inspect or intervene in a running implementer:

```bash
inTandem status                      # what it is doing, and why `wait` would return now
inTandem wait                        # blocks until something needs you
inTandem pending                     # what it is waiting on
inTandem allow                       # or: inTandem deny --message "leave that file alone"
inTandem log                         # the journal
inTandem cost
```

To run a task without MCP, from a script or a shell-based planner:

```bash
inTandem serve ~/work/my-task -m openrouter/qwen/qwen3-coder -p instruction.md
```

In another shell:

```bash
inTandem spec S-1 --file spec.md     # fresh session, spec delivered (-m/--agent: per spec)
inTandem wait                        # exit code says why it returned
inTandem result                      # the implementer's own verdict
inTandem diff                        # inspect the changed files
inTandem directive S-1 D-1 correct "Use the existing retry helper."
inTandem spec S-2 --file next.md     # requirements changed: new id, new session
inTandem finish
```

The CLI verbs map directly to MCP tools: `spec` maps to `submit_spec`, `directive` to
`send_directive`, and `allow`/`deny` to `reply_permission`.

`inTandem attach` opens OpenCode's TUI for the live session. `inTandem attach --web` prints a
URL and login for OpenCode's web interface. Use `say` or `directive` to steer the work; prompts
entered directly in OpenCode do not appear in the inTandem journal.

When one supervisor is running, the CLI finds it without flags. When several are running, pass
`--task` with a name prefix, task directory, or state directory.

## Exit codes

`inTandem wait` returns when the implementer needs attention. Its exit code identifies the
current state, so scripts can make a decision without parsing free-form output. The MCP `wait`
tool returns the same `reason` with a `next_step`.

| exit | reason | do this |
|---|---|---|
| 0 | `result` | review `inTandem result --json`, then verify it yourself |
| 0 | `turn_ended` | the turn ended without a result; read `inTandem msg 3` |
| 5 | `blocked` | `inTandem pending`, then `allow`, `deny --message`, or `answer` |
| 8 | `reasoning_stall` | text, no tools, for 12 minutes: usually a contradiction in the spec |
| 4 | `hung` | nothing for 5 minutes, or one tool silent for 12 |
| 6 | `error` or `protected_violation` | provider error, or a protected file changed |
| 7 | `stopped` | the supervisor is shutting down |
| 124 | `timeout` | still working; wait again |

`wait` is level-triggered: it reports the current state rather than the moment it began. A
planner can therefore call it in a loop without missing a change. Results are reported after
the turn ends, not when a file first appears, so the planner reviews a stable set of changes.

[`skills/intandem-planner/SKILL.md`](../skills/intandem-planner/SKILL.md) is the planner loop written
as an Agent Skill for Claude Code or Codex.

## Alerts

Permission requests, questions, provider errors, stalls and protected-file violations are
escalated. Completed turns are returned through the planner's `wait` call instead of becoming
alerts, keeping notifications focused on work that needs a response. Every alert is recorded
in `notifications.log`; delivery is opt-in:

```bash
inTandem serve ... --notify-cmd 'notify-send "inTandem" "$INTANDEM_NOTIFY"'
inTandem serve ... --ntfy-topic my-private-topic     # publishes summaries to ntfy.sh
```

The command receives `$INTANDEM_NOTIFY`, `$INTANDEM_KIND`, `$INTANDEM_EVENT` (JSON), and
`$INTANDEM_TASK`. inTandem terminates it after 30 seconds. Events larger than 64 KiB arrive as
a stub with `truncated` set because Linux limits environment-variable size.

A stall alert identifies the stuck tool but omits its command, which could contain a secret. To
see the command, use `inTandem status` or the dashboard.

## Configuration

Every `inTandem serve` option can also be set with an environment variable using the
`INTANDEM_` prefix:

| variable | default | |
|---|---|---|
| `INTANDEM_MODEL` | OpenCode's default | `provider/model` for the implementer |
| `INTANDEM_PROTECTED` | `[]` | JSON list of globs, e.g. `'["instruction.md","docs/*.md"]'` |
| `INTANDEM_NOTIFY_CMD`, `INTANDEM_NTFY_TOPIC` | off | alert delivery |
| `INTANDEM_STALL_AFTER` | `720` | seconds of text without a tool call |
| `INTANDEM_QUIET_AFTER` | `300` | seconds with neither |
| `INTANDEM_STATE_ROOT` | `$XDG_STATE_HOME/intandem` | where task state lives |
| `INTANDEM_OPENCODE_BIN` | `opencode` | the OpenCode binary |
| `INTANDEM_WORKDIR` | the MCP server's working directory | the project `inTandem mcp` acts on by default |

## Limitations

- OpenCode 1.x is currently supported.
- A task has one active specification at a time. Run parallel work in separate worktrees.
- `list_models` reports what OpenCode lists, not necessarily what the connected account can use.
  For example, a ChatGPT login may reject some OpenAI models; `wait` reports that as `error`.
- `wait` reasons are inferred from the event stream. Their thresholds are configurable, and
  `inTandem status` displays the values used for each call.
