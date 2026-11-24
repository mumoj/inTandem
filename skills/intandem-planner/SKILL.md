---
name: intandem-planner
description: Plan, submit, monitor, and review tasks delegated to an implementer (OpenCode, Claude Code, or Antigravity) via inTandem. Use when delegating implementation work through inTandem or when checking on, unblocking, or reviewing an active session.
---

# Planning work with inTandem

When delegating through inTandem, you define the task specification and review the result while an implementer (OpenCode, Claude Code, or Antigravity) makes the code changes. Keep specifications scoped to a clear outcome, monitor the session for prompts or stalls, and verify the changed files and test output yourself instead of relying on the implementer's summary.

Prefer the inTandem MCP tools over the CLI whenever they are available. Both expose the same workflow:

- `submit_spec` -> `inTandem spec`
- `wait` -> `inTandem wait`
- `get_result` -> `inTandem result` and `inTandem diff`
- `get_pending` -> `inTandem pending`
- `reply_permission` -> `inTandem allow` / `inTandem deny`
- `answer_question` -> `inTandem answer`
- `send_directive` -> `inTandem directive`
- `finish` -> `inTandem finish`

Pass the `dashboard` URL from the first response (or the `overview` URL when running multiple implementers in parallel) to the user so they can monitor progress in a browser.

## Selecting a harness and model

- The default harness is `opencode` (1.x or 2.x). To use another harness, pass `harness="claude"` (runs Claude Code using the local CLI login) or `harness="antigravity"` (runs `agy` using its sign-in) to `start_implementer`, `spawn_implementer`, or `submit_spec`.
- Call `list_models()` (optionally passing `harness="claude"` or `harness="antigravity"`) to check available models and agents.
- Model and agent formats by harness:
  - **OpenCode**: `provider/model` and an optional `agent` (such as `build` or `plan`).
  - **Claude Code**: a bare model name (such as `claude-sonnet-4-6`) or `anthropic/<model>`, with `agent` unset.
  - **Antigravity**: a bare `agy` model ID (such as `gemini-4-neon-high`) or `antigravity/<model>`, with `agent` set to an `agy` mode (`default`, `accept-edits`, `auto`, or `plan`).

## Standard loop

1. Write a specification covering a single outcome, which files may be modified, and how to verify completion. Make sure the requirements do not contradict each other, since contradictions often show up as reasoning stalls rather than explicit errors.
2. Submit the spec with `submit_spec` (or `inTandem spec S-<n> --file spec.md`). Spec IDs are immutable, so use a new ID whenever requirements change. For independent pieces of work, start separate worktrees with `spawn_implementer` and monitor them together using `wait_any`.
3. Call `wait` (or `inTandem wait --timeout 900`) and handle the returned reason or exit code:

   | Exit code / reason | Meaning | Next step |
   | --- | --- | --- |
   | `0` `result` | Result file written and turn completed | Proceed to step 4 |
   | `0` `turn_ended` | Turn ended without writing a result file | Inspect `get_messages` (`inTandem msg 3`), then send `say` or a directive |
   | `5` `blocked` | Pending permission request or question | Check `get_pending` (`inTandem pending`) and reply |
   | `8` `reasoning_stall` | Emitting text without calling tools | Read `get_messages` and check the spec for ambiguity or contradictions |
   | `4` `hung` | No text or tool activity | Check `status`; abort and re-prompt if stuck |
   | `6` | Provider error or protected file modified | Check `status` and `inTandem log 20` |
   | `124` | Timed out while still working | Call `wait` again |

4. Inspect the result with `get_result` (or `inTandem result --json` and `inTandem diff`). Read the modified files and run the relevant checks yourself before accepting the work.
5. Follow up based on the review:
   - To fix an issue within the same spec, send a `CORRECT` directive via `send_directive` (`inTandem directive S-<n> D-<m> correct ...`).
   - If the requirements themselves changed, stop the active spec (`STOP_AND_WAIT`) and submit a new spec ID.
   - For parallel worktrees, use `land_implementer` with `review` first, then `merge` or `discard`.

## Handling blocked requests

- Use `reply_permission("allow")` (`inTandem allow`) for one-off approvals; only use `always` when the permission should remain granted for the rest of the session.
- Use `reply_permission("deny", message="...")` (`inTandem deny --message "..."`) to reject a request and explain why to the implementer.
- Use `answer_question` (`inTandem answer <label>`) to select an option for a pending question.
- Do not approve edits to protected files; deny the request and direct the implementer to another approach.

## Handling reasoning stalls

Read the recent messages before aborting or restarting. A reasoning stall usually means the specification is underspecified or self-contradictory. Clarify the spec or send a corrective directive instead of simply retrying with the same prompt.

## Notes

- Never edit protected files yourself to make the implementer's changes pass.
- Include actual command output in reviews rather than summarizing it.
- Call `finish` (`inTandem finish`) once the task is complete so the supervisor saves final session logs and shuts down cleanly.
