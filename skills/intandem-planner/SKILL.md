---
name: intandem-planner
description: Plan, submit, monitor, and review work delegated to an OpenCode implementation model through inTandem. Use when a task is delegated to inTandem, or when asked to check on, unblock, or review that work.
---

# Planning work with an inTandem implementer

You are responsible for defining the task and accepting the result. The implementation model in
OpenCode writes the code. Give it a bounded specification, monitor its progress, and inspect the
changes yourself rather than relying on its summary.

When inTandem's MCP tools are available, use them instead of the commands below. The workflow is
the same: `submit_spec`, `wait`, `get_result`, `get_pending`, `reply_permission`,
`answer_question`, `send_directive`, and `finish` map to `spec`, `wait`, `result` and `diff`,
`pending`, `allow`/`deny`, `answer`, `directive`, and `finish`. Share the `dashboard` link from
the first reply with the person overseeing the task.

## Workflow

1. **Write the specification to a file.** State one outcome, the files it may change, and how
   to verify completion. Make requirements precise and consistent; contradictory requirements
   often appear as a reasoning stall rather than a clear error.
2. **Submit it.** `inTandem spec S-<n> --file spec.md`. Spec IDs are immutable; a changed
   requirement is a new ID.
3. **Wait for a state change.** Run `inTandem wait --timeout 900` and branch on its exit code:

   | exit | meaning | next |
   |---|---|---|
   | 0 `result` | it wrote a result and the turn is over | step 4 |
   | 0 `turn_ended` | it stopped without a result | `inTandem msg 3`, then `say` or a directive |
   | 5 `blocked` | a permission or question is pending | `inTandem pending`, then decide |
   | 8 `reasoning_stall` | thinking, not acting | `inTandem msg 3`; look for a contradiction in your spec |
   | 4 `hung` | nothing arriving | `inTandem status`; abort and re-prompt if it is stuck |
   | 6 | provider error or protected file changed | `inTandem status`, `inTandem log 20` |
   | 124 | still working | wait again |

4. **Review the changes.** `inTandem result --json` reports the implementer's conclusion; use
   `inTandem diff`, read the changed files, and run the relevant checks before accepting it.
5. **Correct or continue.** For a correction to the active spec, run
   `inTandem directive S-<n> D-<m> correct --file fix.md`. If the requirement itself is wrong,
   stop the spec with `inTandem directive S-<n> D-<m> stop "..."` and submit a new one.

## Resolve blocked work

- `inTandem allow` grants one requested permission. Use it instead of `always` when the
  permission should not apply to the entire session.
- `inTandem deny --message "why"` rejects a request and sends the reason to the implementer.
- `inTandem answer <label>` selects an option for a pending question.
- A protected-file edit cannot be approved; deny it and choose a different approach.

## Respond to a reasoning stall

Read the latest messages before restarting the work. A reasoning stall often indicates an
unclear or contradictory requirement. Clarify the specification or send a directive when that is
the cause; restarting without changing the context usually repeats the same problem.

## Working boundaries

- Do not edit protected files to make the implementer's work pass.
- Include command output in reviews rather than paraphrasing it.
- Call `inTandem finish` when the task is complete so the session records its final evidence.
