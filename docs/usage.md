# Using inTandem

## Use it from Claude Code or Codex

Register inTandem once as an MCP server:

```bash
claude mcp add inTandem --scope user -- inTandem mcp      # Claude Code
codex mcp add inTandem -- inTandem mcp                    # Codex
```

That's all the setup there is. Ask the planner to delegate work, for example "have a cheaper
model write the tests for citations.py through inTandem", and it uses the tools:
`submit_spec`, `wait`, `get_result`, `get_pending`, `reply_permission`, `send_directive`,
`finish`, and the rest. The server's instructions teach it the planner loop.

- **Nothing to start.** When the planner connects, `inTandem mcp` starts one OpenCode server in
  the background, and the first `submit_spec` starts a supervisor for the planner's project.
  `start_implementer` is only for setting a default model or protected files.
- **Lives as long as the planner.** Everything runs in the `inTandem mcp` process and its one
  OpenCode child. When the planner disconnects or exits, every supervisor finishes, capturing
  its evidence, and OpenCode stops. State stays on disk, so the next session resumes it.
- **Any implementer, per spec.** `submit_spec` takes `model` (`provider/model`) and `agent`
  (`build`, `plan`, or one from your OpenCode config). `list_models` shows what is available.
- **Subagents count.** When the implementer delegates to an OpenCode subagent, the subagent's
  permission requests block the spec and its activity keeps the spec alive. Protected files
  are enforced in its session too.
- **Several implementers at once.** For independent pieces of one task, the planner calls
  `spawn_implementer` once per piece. See below.

## Parallel implementers

Like a planner's own subagents, but each one is a cheaper model with its own guardrails:

```text
spawn_implementer(name="farewell", text=..., model="openai/gpt-5.6-luna")
spawn_implementer(name="subtract", text=..., model="openai/gpt-5.6-luna")
wait_any()                                   # wakes when either one needs the planner
land_implementer(name="farewell")            # review: the diff against where it forked
land_implementer(name="farewell", action="merge")
```

- **Isolated where it matters.** Each implementer works on its own branch, `intandem/<name>`, in
  a git worktree under the state root, outside the repository, with its own supervisor,
  journal and protected files. The planner's working tree stays clean.
- **One wait for all of them.** `wait_any` returns as soon as any implementer finishes, blocks
  or stops, and lists every one that needs attention. While it waits, it announces each
  change as an MCP progress message ("farewell finished S-1: result"), so the planner's UI
  shows an implementer ending as it happens.
- **Addressed by name.** Every other tool takes the implementer's name as `workdir`:
  `get_pending(workdir="farewell")`, `send_directive(..., workdir="farewell")`.
- **Landing is explicit.** `review` shows committed and uncommitted changes against the fork
  point. `merge` commits what the implementer left and merges it into the current branch, and
  a conflicting merge is aborted, leaving the repository untouched. `discard` throws the work
  away. Both stop the supervisor, remove the worktree and branch, and keep its state under
  `landed/` as evidence. Files left behind by running checks, such as `__pycache__`, are
  never committed.
- **Cheap to add.** Implementers share [one process and one OpenCode server](architecture.md#one-process-one-opencode), so a
  new one starts in a few seconds once the session's OpenCode is up.

## Watch it in a browser

Every tool reply carries a `dashboard` link, plus an `overview` link to the page that lists
every implementer of the session. `inTandem serve` prints its dashboard link on startup. The page
shows what the implementer is doing and why `wait` would return right now, its latest
messages, the result and changed files, spend, and a timeline of everything in the journal.
Permission requests and questions appear with **Allow**/**Deny** and answer buttons, so the
human can unblock a run without a terminal. **Open in OpenCode** opens OpenCode's own web UI on
the same server. The access token travels in the link's `#fragment`, which browsers never send
to a server.

## The CLI: for humans and scripts

A planner should use the MCP tools. The CLI drives the same bridge, so it is for everything
else:

- **Stepping in from a terminal.** Over SSH from a phone, with no browser and no open port:
  see what is blocked, allow or deny it, and check on spend.
- **Scripts and CI.** Exit codes carry the reason (see [Exit codes](#exit-codes)), and
  `--json` gives the raw response, so a shell loop can drive an implementer with no planner.
- **Planners without MCP.** Anything that can run a shell command can use inTandem.
- **A supervisor of your own.** `inTandem serve` runs one task on a hub of its own, with its own
  OpenCode server, outside any planner session.

Stepping in on a running implementer, whoever started it:

```bash
inTandem status                      # what it is doing, and why `wait` would return now
inTandem wait                        # blocks until something needs you
inTandem pending                     # what it is waiting on
inTandem allow                       # or: inTandem deny --message "leave that file alone"
inTandem log                         # the journal
inTandem cost
```

Running a task without MCP, from a script or a planner that shells out:

```bash
inTandem serve ~/work/my-task -m openrouter/qwen/qwen3-coder -p instruction.md
```

In another shell:

```bash
inTandem spec S-1 --file spec.md     # fresh session, spec delivered (-m/--agent: per spec)
inTandem wait                        # exit code says why it returned
inTandem result                      # the implementer's own verdict
inTandem diff                        # what it actually changed: check, don't trust
inTandem directive S-1 D-1 correct "Use the existing retry helper."
inTandem spec S-2 --file next.md     # requirements changed: new id, new session
inTandem finish
```

These verbs mirror the MCP tools one for one: `spec` is `submit_spec`, `directive` is
`send_directive`, `allow`/`deny` are `reply_permission`, and so on.

To watch the implementer work, `inTandem attach` opens OpenCode's own TUI on the live session,
and `inTandem attach --web` prints a URL and login for OpenCode's web UI. Watch there, but steer
with `say` or `directive`: a prompt typed into OpenCode never reaches the journal.

With one supervisor running, no flags are needed to find it. With several, pass `--task`
with a name prefix, the task directory, or the state directory.

## Exit codes

`inTandem wait` returns when the implementer needs attention, and its exit code says why, so a
script can branch without parsing anything. The MCP `wait` tool returns the same `reason`
with a `next_step`.

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

`wait` is level-triggered: it reports the current state, not the moment a state began, so a
planner that calls it in a loop cannot miss a wakeup. A result is reported once the turn is
over, not when the file lands, so the planner never reviews a moving target.

[`skills/intandem-planner/SKILL.md`](../skills/intandem-planner/SKILL.md) is the planner loop written
as an Agent Skill for Claude Code or Codex.

## Alerts

Permission requests, questions, provider errors, stalls and protected-file violations are
escalated. Finished turns are not: they are for the planner's `wait`, and a notification
nobody needed trains people to ignore the channel. Every alert is recorded in
`notifications.log`; delivery is opt-in:

```bash
inTandem serve ... --notify-cmd 'notify-send "inTandem" "$INTANDEM_NOTIFY"'
inTandem serve ... --ntfy-topic my-private-topic     # publishes summaries to ntfy.sh
```

The command gets `$INTANDEM_NOTIFY`, `$INTANDEM_KIND`, `$INTANDEM_EVENT` (JSON) and `$INTANDEM_TASK`,
and is killed if it runs longer than 30 seconds. An event over 64 KiB arrives as a stub with
`truncated` set, because Linux caps the size of an environment variable.

A stall alert names the stuck tool but never its command, which could hold a secret. To see the
command, use `inTandem status` or the dashboard.

## Configuration

Every option of `inTandem serve` can also come from the environment with an `INTANDEM_` prefix:

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

- OpenCode 1.x only, for now.
- One active spec per task at a time, by design. Parallel work means one worktree each.
- `list_models` shows what OpenCode lists, not what an account accepts: a ChatGPT login
  refuses some OpenAI models, and that surfaces as `error` from `wait`.
- `wait` reasons are heuristics over the event stream. The thresholds are configurable,
  and `inTandem status` shows the numbers behind each call.
