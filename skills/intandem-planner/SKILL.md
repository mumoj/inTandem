---
name: intandem-planner
description: Plan, submit, monitor, and review work delegated to an implementation model (OpenCode or Claude Code) through inTandem. Use when a task is delegated to inTandem, or when asked to check on, unblock, or review that work.
---

# Planning work with an inTandem implementer

You are responsible for defining the task and accepting the result. The implementer (running on
OpenCode or Claude Code) writes the code. Give it a bounded specification, monitor its progress,
and inspect the changes yourself rather than relying on its summary.

When inTandem's MCP tools are available, use them instead of the CLI commands below. The workflow
is the same: `submit_spec`, `wait`, `get_result`, `get_pending`, `reply_permission`,
`answer_question`, `send_directive`, and `finish` map to `spec`, `wait`, `result` and `diff`,
`pending`, `allow`/`deny`, `answer`, `directive`, and `finish`. Share the `dashboard` link from
the first reply (or the `overview` link for parallel implementers) with the person overseeing the
task.

## Choosing a harness and model

- Default harness is `opencode` (1.x or 2.x). Pass `harness="claude"` on `start_implementer`,
  `spawn_implementer`, or `submit_spec` to run Claude Code on the user's subscription login.
- Use `list_models()` (or `list_models(harness="claude")`) to see available models and agents.
- OpenCode takes `provider/model` and optional `agent` (e.g. `build`, `plan`). Claude Code takes
  bare model names (e.g. `claude-sonnet-4-6`) or `anthropic/<model>`, and no `agent`.

## Workflow

1. **Write the specification.** State one outcome, the files it may change, and how to verify
   completion. Make requirements precise and consistent; contradictory requirements often appear
   as a reasoning stall rather than a clear error.
2. **Submit it.** `submit_spec` (or `inTandem spec S-<n> --file spec.md`). Spec IDs are
   immutable; a changed requirement is a new ID. For independent pieces of work, use
   `spawn_implementer` once per piece and wait with `wait_any`.
3. **Wait for a state change.** Call `wait` (or `inTandem wait --timeout 900`) and branch on the
   reason / exit code:

   | exit | meaning | next |
   |---|---|---|
   | 0 `result` | it wrote a result and the turn is over | step 4 |
   | 0 `turn_ended` | it stopped without a result | `get_messages` / `inTandem msg 3`, then `say` or a directive |
   | 5 `blocked` | a permission or question is pending | `get_pending` / `inTandem pending`, then decide |
   | 8 `reasoning_stall` | thinking, not acting | `get_messages`; look for a contradiction in your spec |
   | 4 `hung` | nothing arriving | `status`; abort and re-prompt if it is stuck |
   | 6 | provider error or protected file changed | `status`, `inTandem log 20` |
   | 124 | still working | wait again |

4. **Review the changes.** `get_result` (or `inTandem result --json` + `inTandem diff`) reports
   the implementer's claim and changed files. Read the files and run the relevant checks yourself
   before accepting it.
5. **Correct, continue, or land.** For a correction to the active spec, send a `CORRECT` directive
   (`send_directive` / `inTandem directive S-<n> D-<m> correct ...`). If the requirement itself is
   wrong, stop the spec (`STOP_AND_WAIT`) and submit a new spec ID. For parallel worktrees, use
   `land_implementer` (`review`, then `merge` or `discard`).

## Resolve blocked work

- `reply_permission("allow")` / `inTandem allow` grants one requested permission. Prefer it over
  `always` when the permission should not apply to the entire session.
- `reply_permission("deny", message="why")` / `inTandem deny --message "why"` rejects a request
  and sends the reason back to the implementer.
- `answer_question` / `inTandem answer <label>` selects an option for a pending question.
- Never allow an edit to a protected file; deny it and choose a different approach.

## Respond to a reasoning stall

Read the latest messages before restarting the work. A reasoning stall often indicates an
unclear or contradictory requirement. Clarify the specification or send a directive when that is
the cause; restarting without changing the context usually repeats the same problem.

## Working boundaries

- Do not edit protected files to make the implementer's work pass.
- Include command output in reviews rather than paraphrasing it.
- Call `finish` / `inTandem finish` when the task is complete so the session records its final
  evidence.
